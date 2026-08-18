import base64
import hashlib
import json
import os
import re
import threading
import uuid
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash
import pytest


TEST_PASSWORD = "Teste-Seguro-2026!"
os.environ["SECRET_KEY"] = "test-session-secret"
os.environ["DATA_ENCRYPTION_SECRET"] = "test-data-secret"
os.environ["RATE_LIMIT_SECRET"] = "test-rate-secret"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD_HASH"] = generate_password_hash(TEST_PASSWORD)
# Nunca permita que a suíte use por engano um DATABASE_URL herdado da produção.
os.environ["DATABASE_URL"] = os.path.abspath("instance/test-contracts.db")
os.environ.pop("VERCEL", None)

import app as application
from storage import AppointmentConflictError


VALID_FORM = {
    "nome_contratante": "Maria da Silva", "cpf_contratante": "529.982.247-25",
    "rg_contratante": "12.345.678-9", "cidade_contratante": "Sorocaba/SP",
    "endereco_contratante": "Rua das Flores", "numero": "123", "complemento": "Sala 1",
    "bairro_contratante": "Centro", "cep_contratante": "18000-000",
    "telefone": "(15) 99999-9999", "email": "maria@example.com",
    "nome_contratada": "Consultório Odontológico Dr. Ângelo G. Martinez",
    "cpf_cnpj_contratada": "11.222.333/0001-81", "cro": "CRO-SP 12345",
    "endereco_clinica": "Avenida Central, 100", "nome_paciente": "João da Silva",
    "procedimentos": "Tratamento odontológico restaurador", "valor_total": "1.250,00",
    "forma_pagamento": "Pix", "parcelas": "À vista", "vencimentos": "Na assinatura",
    "valor_avaliacao": "150,00", "limite_atraso": "15", "cidade_clinica": "Sorocaba/SP",
    "data_contrato": "17/08/2026",
}
VALID_APPOINTMENT = {
    "patient_name": "Carlos Agenda Silva", "cpf": "529.982.247-25",
    "phone": "(15) 99999-9999", "email": "carlos.agenda@example.com",
    "appointment_date": "2026-08-20", "appointment_time": "14:00",
    "appointment_type_id": "consulta", "professional": "Dr. Ângelo G. Martinez",
    "observations": "Paciente de teste da agenda", "status": "AGENDADO",
}
PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(
    base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
).decode()


@pytest.fixture(autouse=True)
def clean_database():
    with application.store.connect() as connection:
        connection.execute("DELETE FROM appointment_events")
        connection.execute("DELETE FROM appointment_search_terms")
        connection.execute("DELETE FROM appointments")
        connection.execute("DELETE FROM contract_events")
        connection.execute("DELETE FROM contracts")
        connection.execute("DELETE FROM rate_limits")


def csrf_from(response):
    match = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', response.data)
    assert match
    return match.group(1).decode()


def login(client, password=TEST_PASSWORD):
    page = client.get("/login")
    return client.post("/login", data={"csrf_token": csrf_from(page), "username": "admin", "password": password})


def create_contract(client, overrides=None):
    login(client)
    page = client.get("/")
    data = dict(VALID_FORM)
    data.update(overrides or {})
    data["csrf_token"] = csrf_from(page)
    response = client.post("/api/contracts", data=data)
    return response


def create_appointment(client, overrides=None):
    login(client)
    page = client.get("/agendamentos")
    data = dict(VALID_APPOINTMENT)
    data.update(overrides or {})
    data["csrf_token"] = csrf_from(page)
    return client.post("/agendamentos/novo", data=data)


def token_from_payload(payload):
    return urlparse(payload["url"]).path.rsplit("/", 1)[1]


def test_admin_requires_login_and_login_flow():
    client = application.app.test_client()
    response = client.get("/")
    assert response.status_code == 302 and "/login" in response.location
    wrong = login(client, "senha-incorreta")
    assert wrong.status_code == 401
    assert "Usuário ou senha incorretos" in wrong.get_data(as_text=True)
    correct = login(client)
    assert correct.status_code == 302 and correct.location.endswith("/")
    admin_page = client.get("/")
    assert admin_page.status_code == 200
    assert admin_page.headers["Cache-Control"] == "no-store, private"
    with client.session_transaction() as session:
        session.clear()
    assert client.get("/").status_code == 302


def test_backend_validation_and_csrf():
    client = application.app.test_client()
    login(client)
    no_csrf = client.post("/api/contracts", data=VALID_FORM)
    assert no_csrf.status_code == 400
    page = client.get("/")
    invalid = dict(VALID_FORM, cpf_contratante="111.111.111-11", telefone="123")
    invalid["csrf_token"] = csrf_from(page)
    response = client.post("/api/contracts", data=invalid)
    assert response.status_code == 400
    assert response.json["campos"]["cpf_contratante"] == "Informe um CPF válido."
    assert "telefone" in response.json["campos"]


def test_contract_token_access_signing_and_reuse_blocked():
    client = application.app.test_client()
    created = create_contract(client)
    assert created.status_code == 201
    payload = created.json
    token = token_from_payload(payload)
    assert len(token) >= 40
    assert "529" not in payload["url"] and "João" not in payload["url"]
    admin_page = client.get("/")
    sent = client.post(f"/api/contracts/{payload['id']}/sent", headers={"X-CSRF-Token": csrf_from(admin_page)})
    assert sent.status_code == 200
    assert sent.json["status"] == "ENVIADO"
    assert application.store.get_by_id(payload["id"])["status"] == "ENVIADO"

    patient = application.app.test_client()
    invalid = patient.get(f"/assinar/{token[:-1]}x")
    assert invalid.status_code == 404 and "Link inválido" in invalid.get_data(as_text=True)
    page = patient.get(f"/assinar/{token}")
    assert page.status_code == 200 and "João da Silva" in page.get_data(as_text=True)
    assert application.store.get_by_id(payload["id"])["status"] == "VISUALIZADO"
    signed = patient.post(f"/assinar/{token}", data={
        "csrf_token": csrf_from(page), "aceite": "sim", "assinatura_paciente": PNG_DATA_URL,
    })
    assert signed.status_code == 200
    assert signed.mimetype == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    reused = patient.get(f"/assinar/{token}")
    assert reused.status_code == 200 and "Contrato já assinado" in reused.get_data(as_text=True)
    assert application.store.get_by_id(payload["id"])["status"] == "ASSINADO"
    assert {event["event_type"] for event in application.store.events_for(payload["id"])} == {
        "CRIADO", "ENVIADO", "VISUALIZADO", "ASSINADO",
    }


def test_expired_contract_is_rejected():
    client = application.app.test_client()
    created = create_contract(client, {"nome_paciente": "Paciente Expirado"})
    payload = created.json
    with application.store.connect() as connection:
        connection.execute(application.store._sql("UPDATE contracts SET expires_at = ? WHERE id = ?"),
                           (application._iso(application._now() - timedelta(minutes=1)), payload["id"]))
    token = token_from_payload(payload)
    response = application.app.test_client().get(f"/assinar/{token}")
    assert response.status_code == 410
    assert "link expirou" in response.get_data(as_text=True)


def test_word_download_and_security_headers():
    client = application.app.test_client()
    login(client)
    page = client.get("/")
    data = dict(VALID_FORM, csrf_token=csrf_from(page))
    response = client.post("/gerar", data=data)
    assert response.status_code == 200
    assert response.mimetype == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    login_page = application.app.test_client().get("/login")
    assert "frame-ancestors 'none'" in login_page.headers["Content-Security-Policy"]
    assert login_page.headers["X-Content-Type-Options"] == "nosniff"


def test_phone_normalization_variants():
    assert application._normalize_phone("(15) 99999-9999") == "5515999999999"
    assert application._normalize_phone("15999999999") == "5515999999999"
    assert application._normalize_phone("+55 15 99999-9999") == "5515999999999"
    assert application._normalize_phone("(15) 3333-4444") == "551533334444"
    assert application._normalize_phone("123") is None


def test_legacy_link_remains_compatible():
    compact = {application.FIELD_CODES[field]: value for field, value in application._safe_form(VALID_FORM).items()}
    raw = b"Z" + zlib.compress(json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode(), 9)
    key = base64.urlsafe_b64encode(hashlib.sha256(b"gerador-contratos-local").digest())
    token = Fernet(key).encrypt(raw).decode()
    response = application.app.test_client().get(f"/assinar/{token}")
    assert response.status_code == 200
    assert "versão anterior" in response.get_data(as_text=True)


def test_tracking_search_filters_history_and_irreversible_statuses():
    admin = application.app.test_client()
    created = create_contract(admin, {"nome_paciente": "Ana Pesquisa Silva"})
    payload = created.json
    contract_id = payload["id"]
    token = token_from_payload(payload)

    tracking = admin.get("/acompanhamento")
    text = tracking.get_data(as_text=True)
    assert tracking.status_code == 200
    assert "Ana Pesquisa Silva" in text
    assert "***.***.***-25" in text
    assert "Aguardando envio" in text
    assert "529.982.247-25" not in text

    sent = admin.post(f"/api/contracts/{contract_id}/sent", headers={"X-CSRF-Token": csrf_from(admin.get('/'))})
    assert sent.json["status"] == "ENVIADO" and sent.json["alterado"] is True
    by_patient = admin.get("/acompanhamento?q=ana+pesquisa")
    assert "Ana Pesquisa Silva" in by_patient.get_data(as_text=True)
    by_number = admin.get(f"/acompanhamento?q={payload['numero_contrato']}")
    assert payload["numero_contrato"] in by_number.get_data(as_text=True)
    by_status = admin.get("/acompanhamento?status=ENVIADO")
    assert "Ana Pesquisa Silva" in by_status.get_data(as_text=True)
    assert "Ana Pesquisa Silva" not in admin.get("/acompanhamento?status=ASSINADO").get_data(as_text=True)

    row = application.store.get_by_id(contract_id)
    local_date = application._parse_iso(row["created_at"]).astimezone(application.BRAZIL_TZ).strftime("%Y-%m-%d")
    by_period = admin.get(f"/acompanhamento?data_de={local_date}&data_ate={local_date}")
    assert "Ana Pesquisa Silva" in by_period.get_data(as_text=True)
    detail = admin.get(f"/acompanhamento/{contract_id}")
    detail_text = detail.get_data(as_text=True)
    assert detail.status_code == 200 and "Linha do tempo" in detail_text
    assert "Contrato criado" in detail_text
    assert "Link enviado" in detail_text

    patient = application.app.test_client()
    sign_page = patient.get(f"/assinar/{token}")
    assert application.store.get_by_id(contract_id)["status"] == "VISUALIZADO"
    late_send = admin.post(f"/api/contracts/{contract_id}/sent", headers={"X-CSRF-Token": csrf_from(admin.get('/'))})
    assert late_send.json["alterado"] is False and late_send.json["status"] == "VISUALIZADO"
    signed = patient.post(f"/assinar/{token}", data={
        "csrf_token": csrf_from(sign_page), "aceite": "sim", "assinatura_paciente": PNG_DATA_URL,
    })
    assert signed.status_code == 200
    assert application.store.get_by_id(contract_id)["status"] == "ASSINADO"
    assert application.store.mark_sent(contract_id, "admin", application._iso(application._now())) is False
    assert application.store.mark_viewed(contract_id, application._iso(application._now())) is False
    events = application.store.events_for(contract_id)
    assert [event["event_type"] for event in events] == ["CRIADO", "ENVIADO", "VISUALIZADO", "ASSINADO"]

    logout_page = admin.get("/")
    admin.post("/logout", data={"csrf_token": csrf_from(logout_page)})
    assert admin.get("/acompanhamento").status_code == 302
    login(admin)
    persisted = admin.get(f"/acompanhamento/{contract_id}")
    assert "Assinado e finalizado" in persisted.get_data(as_text=True)


def test_tracking_pagination_and_expired_filter():
    admin = application.app.test_client()
    login(admin)
    ids = []
    for index in range(21):
        page = admin.get("/")
        data = dict(VALID_FORM, nome_paciente=f"Paciente Paginacao {chr(65 + index)}", csrf_token=csrf_from(page))
        created = admin.post("/api/contracts", data=data)
        assert created.status_code == 201
        ids.append(created.json["id"])
    first_page = admin.get("/acompanhamento")
    first_text = first_page.get_data(as_text=True)
    assert "21 registro(s)" in first_text and "Página 1 de 2" in first_text
    second_page = admin.get("/acompanhamento?pagina=2")
    assert "Página 2 de 2" in second_page.get_data(as_text=True)

    expired_id = ids[0]
    with application.store.connect() as connection:
        connection.execute(application.store._sql("UPDATE contracts SET expires_at = ? WHERE id = ?"),
                           (application._iso(application._now() - timedelta(minutes=1)), expired_id))
    expired_page = admin.get("/acompanhamento?status=EXPIRADO")
    assert "Expirado" in expired_page.get_data(as_text=True)
    expired_events = application.store.events_for(expired_id)
    assert [event["event_type"] for event in expired_events].count("EXPIRADO") == 1
    admin.get("/acompanhamento?status=EXPIRADO")
    assert [event["event_type"] for event in application.store.events_for(expired_id)].count("EXPIRADO") == 1


def test_tracking_routes_require_authentication():
    client = application.app.test_client()
    assert client.get("/acompanhamento").status_code == 302
    assert client.get("/acompanhamento/not-a-uuid").status_code == 302


def test_contract_soft_delete_hides_record_revokes_link_and_preserves_audit():
    admin = application.app.test_client()
    created = create_contract(admin, {"nome_paciente": "Contrato Teste Excluir"})
    payload = created.json
    contract_id = payload["id"]
    token = token_from_payload(payload)

    detail = admin.get(f"/acompanhamento/{contract_id}")
    deleted = admin.post(
        f"/acompanhamento/{contract_id}/excluir",
        data={"csrf_token": csrf_from(detail)},
        follow_redirects=True,
    )
    page = deleted.get_data(as_text=True)
    assert deleted.status_code == 200
    assert "Contrato excluído da lista" in page
    assert "Contrato Teste Excluir" not in page
    assert application.store.get_by_id(contract_id) is None
    assert admin.get(f"/acompanhamento/{contract_id}").status_code == 404
    invalid_link = application.app.test_client().get(f"/assinar/{token}")
    assert invalid_link.status_code == 404 and "Link inválido" in invalid_link.get_data(as_text=True)

    with application.store.connect() as connection:
        stored = connection.execute(
            "SELECT status, deleted_at, data_ciphertext FROM contracts WHERE id = ?", (contract_id,)
        ).fetchone()
    assert stored["deleted_at"] and stored["data_ciphertext"]
    assert [event["event_type"] for event in application.store.events_for(contract_id)] == ["CRIADO", "EXCLUIDO"]


def test_contract_delete_requires_authentication_and_csrf():
    anonymous = application.app.test_client()
    assert anonymous.post(f"/acompanhamento/{uuid.uuid4()}/excluir").status_code == 302
    admin = application.app.test_client()
    created = create_contract(admin)
    assert admin.post(f"/acompanhamento/{created.json['id']}/excluir").status_code == 400


def test_appointment_creation_validation_encryption_and_conflict():
    anonymous = application.app.test_client()
    assert anonymous.get("/agendamentos").status_code == 302
    admin = application.app.test_client()
    login(admin)
    page = admin.get("/agendamentos")
    assert page.status_code == 200 and "Novo agendamento" in page.get_data(as_text=True)

    invalid = dict(VALID_APPOINTMENT, cpf="111.111.111-11", phone="123", csrf_token=csrf_from(page))
    invalid_response = admin.post("/agendamentos/novo", data=invalid, follow_redirects=True)
    assert "Informe um CPF válido" in invalid_response.get_data(as_text=True)
    assert application.store.list_appointments() == []

    created = admin.post("/agendamentos/novo", data=dict(VALID_APPOINTMENT, csrf_token=csrf_from(admin.get('/agendamentos'))))
    assert created.status_code == 302 and "/agendamentos/" in created.location
    appointment_id = created.location.rsplit("/", 1)[1]
    row = application.store.get_appointment(appointment_id)
    assert row["status"] == "AGENDADO"
    assert "Carlos Agenda Silva" not in row["data_ciphertext"]
    assert [event["event_type"] for event in application.store.appointment_events(appointment_id)] == ["CRIADO"]

    conflict_data = dict(VALID_APPOINTMENT, patient_name="Outro Paciente Silva",
                         professional="dr. ângelo g. martinez", csrf_token=csrf_from(admin.get('/agendamentos')))
    conflict = admin.post("/agendamentos/novo", data=conflict_data, follow_redirects=True)
    assert "Este horário já possui um atendimento agendado" in conflict.get_data(as_text=True)
    assert len(application.store.list_appointments()) == 1


def test_appointment_slot_is_atomic_for_simultaneous_requests():
    barrier = threading.Barrier(2)

    def attempt(patient_name):
        data = dict(VALID_APPOINTMENT, patient_name=patient_name)
        timestamp = application._iso(application._now())
        record = {
            "id": str(uuid.uuid4()), "data_ciphertext": application._encrypt_appointment(data),
            "appointment_date": data["appointment_date"], "appointment_time": data["appointment_time"],
            "appointment_type_id": data["appointment_type_id"], "professional": data["professional"],
            "professional_key": application._professional_key(data["professional"]),
            "status": data["status"], "created_by": "admin", "created_at": timestamp,
            "updated_at": timestamp, "contract_id": None,
        }
        barrier.wait()
        try:
            application.store.create_appointment(record, application._appointment_search_tokens(data))
            return "created"
        except AppointmentConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, ("Paciente Simultâneo Um", "Paciente Simultâneo Dois")))
    assert sorted(results) == ["conflict", "created"]
    assert len(application.store.list_appointments()) == 1


def test_appointment_edit_status_cancel_history_and_persistence():
    admin = application.app.test_client()
    created = create_appointment(admin)
    appointment_id = created.location.rsplit("/", 1)[1]
    detail = admin.get(created.location)
    assert "Carlos Agenda Silva" in detail.get_data(as_text=True)
    edit_data = dict(VALID_APPOINTMENT, appointment_date="2026-08-21", appointment_time="15:30",
                     appointment_type_id="retorno", phone="+55 15 99999-9999", status="CONFIRMADO",
                     csrf_token=csrf_from(detail))
    edited = admin.post(f"/agendamentos/{appointment_id}/editar", data=edit_data)
    assert edited.status_code == 302
    row = application.store.get_appointment(appointment_id)
    assert (row["appointment_date"], row["appointment_time"], row["status"]) == ("2026-08-21", "15:30", "CONFIRMADO")
    events = application.store.appointment_events(appointment_id)
    assert {event["event_type"] for event in events} >= {"CRIADO", "HORARIO_ALTERADO", "CONFIRMADO", "ATUALIZADO"}
    event_types = [event["event_type"] for event in events]
    assert event_types.index("HORARIO_ALTERADO") < event_types.index("CONFIRMADO") < event_types.index("ATUALIZADO")
    assert any("14:00" in event["description"] and "15:30" in event["description"] for event in events)

    status_page = admin.get(f"/agendamentos/{appointment_id}")
    concluded = admin.post(f"/agendamentos/{appointment_id}/status",
                           data={"csrf_token": csrf_from(status_page), "status": "CONCLUIDO"})
    assert concluded.status_code == 302 and application.store.get_appointment(appointment_id)["status"] == "CONCLUIDO"
    cancel_page = admin.get(f"/agendamentos/{appointment_id}")
    cancelled = admin.post(f"/agendamentos/{appointment_id}/status",
                           data={"csrf_token": csrf_from(cancel_page), "status": "CANCELADO"})
    assert cancelled.status_code == 302
    assert application.store.get_appointment(appointment_id)["status"] == "CANCELADO"
    assert application.store.get_appointment(appointment_id) is not None
    history = admin.get(f"/agendamentos/{appointment_id}").get_data(as_text=True)
    assert "Horário alterado" in history and "Atendimento concluído" in history and "Agendamento cancelado" in history
    assert "Enviar confirmação pelo WhatsApp" not in history

    logout_page = admin.get("/")
    admin.post("/logout", data={"csrf_token": csrf_from(logout_page)})
    assert admin.get(f"/agendamentos/{appointment_id}").status_code == 302
    login(admin)
    assert "Cancelado" in admin.get(f"/agendamentos/{appointment_id}").get_data(as_text=True)


def test_appointment_calendar_list_search_filters_indicators_and_whatsapp_history():
    admin = application.app.test_client()
    login(admin)
    today = application._now().astimezone(application.BRAZIL_TZ).date().isoformat()
    records = [
        dict(VALID_APPOINTMENT, patient_name="Ana Calendario Souza", appointment_date=today,
             appointment_time="08:00", status="CONFIRMADO"),
        dict(VALID_APPOINTMENT, patient_name="Bruno Agenda Lima", cpf="111.444.777-35",
             phone="(15) 3333-4444", appointment_date=today, appointment_time="09:30",
             appointment_type_id="retorno", status="AGUARDANDO"),
        dict(VALID_APPOINTMENT, patient_name="Clara Atendimento Rocha", cpf="935.411.347-80",
             phone="(15) 98888-7777", appointment_date=today, appointment_time="11:00",
             appointment_type_id="avaliacao", status="CONCLUIDO"),
    ]
    ids = []
    for record in records:
        record["csrf_token"] = csrf_from(admin.get("/agendamentos"))
        response = admin.post("/agendamentos/novo", data=record)
        assert response.status_code == 302
        ids.append(response.location.rsplit("/", 1)[1])

    for view in ("day", "week", "month", "list"):
        response = admin.get(f"/agendamentos?visao={view}&data={today}")
        assert response.status_code == 200 and "Ana Calendario Souza" in response.get_data(as_text=True)
    page = admin.get(f"/agendamentos?visao=day&data={today}").get_data(as_text=True)
    assert page.index("08:00") < page.index("09:30") < page.index("11:00")
    assert ">3<" in page and "Confirmados" in page and "Aguardando" in page and "Concluídos" in page
    assert "Ana Calendario Souza" in admin.get(f"/agendamentos?visao=list&data={today}&q=Ana+Calendario").get_data(as_text=True)
    assert "Bruno Agenda Lima" in admin.get(f"/agendamentos?visao=list&data={today}&q=111.444.777-35").get_data(as_text=True)
    assert "Bruno Agenda Lima" in admin.get(f"/agendamentos?visao=list&data={today}&q=1533334444").get_data(as_text=True)
    filtered = admin.get(f"/agendamentos?visao=list&data={today}&status=CONFIRMADO&tipo=consulta")
    filtered_list = filtered.get_data(as_text=True).split('class="appointment-list"', 1)[1]
    assert "Ana Calendario Souza" in filtered_list and "Bruno Agenda Lima" not in filtered_list

    detail = admin.get(f"/agendamentos/{ids[0]}")
    assert "Seu atendimento com o Dr. Ângelo G. Martinez está agendado" in detail.get_data(as_text=True)
    sent = admin.post(f"/api/appointments/{ids[0]}/confirmation-sent",
                      headers={"X-CSRF-Token": csrf_from(detail)})
    assert sent.status_code == 200
    assert "CONFIRMACAO_ENVIADA" in {event["event_type"] for event in application.store.appointment_events(ids[0])}


def test_appointment_empty_invalid_and_edit_conflict():
    admin = application.app.test_client()
    login(admin)
    page = admin.get("/agendamentos")
    empty = admin.post("/agendamentos/novo", data={"csrf_token": csrf_from(page)}, follow_redirects=True)
    assert "Informe nome completo do paciente" in empty.get_data(as_text=True)
    invalid = dict(VALID_APPOINTMENT, appointment_date="data", appointment_time="25:90",
                   phone="00000000000", csrf_token=csrf_from(admin.get('/agendamentos')))
    invalid_result = admin.post("/agendamentos/novo", data=invalid, follow_redirects=True)
    assert "telefone válido" in invalid_result.get_data(as_text=True)

    first = admin.post("/agendamentos/novo", data=dict(VALID_APPOINTMENT, csrf_token=csrf_from(admin.get('/agendamentos'))))
    second_data = dict(VALID_APPOINTMENT, patient_name="Segundo Paciente Teste", appointment_time="16:00",
                       cpf="111.444.777-35", csrf_token=csrf_from(admin.get('/agendamentos')))
    second = admin.post("/agendamentos/novo", data=second_data)
    second_id = second.location.rsplit("/", 1)[1]
    edit_page = admin.get(f"/agendamentos/{second_id}")
    conflict_edit = dict(second_data, appointment_time="14:00", csrf_token=csrf_from(edit_page))
    response = admin.post(f"/agendamentos/{second_id}/editar", data=conflict_edit, follow_redirects=True)
    assert "Este horário já possui um atendimento agendado" in response.get_data(as_text=True)
    assert application.store.get_appointment(second_id)["appointment_time"] == "16:00"


def test_appointment_soft_delete_hides_record_preserves_audit_and_releases_slot():
    admin = application.app.test_client()
    created = create_appointment(admin)
    appointment_id = created.location.rsplit("/", 1)[1]
    detail = admin.get(created.location)
    deleted = admin.post(
        f"/agendamentos/{appointment_id}/excluir",
        data={"csrf_token": csrf_from(detail)},
        follow_redirects=True,
    )
    page = deleted.get_data(as_text=True)
    assert deleted.status_code == 200
    assert "Agendamento excluído da agenda" in page
    assert "Carlos Agenda Silva" not in page
    assert application.store.get_appointment(appointment_id) is None
    assert application.store.list_appointments() == []
    with application.store.connect() as connection:
        stored = connection.execute(
            "SELECT status, deleted_at FROM appointments WHERE id = ?", (appointment_id,)
        ).fetchone()
    assert stored["status"] == "CANCELADO" and stored["deleted_at"]
    assert "EXCLUIDO" in {
        event["event_type"] for event in application.store.appointment_events(appointment_id)
    }
    assert admin.get(f"/agendamentos/{appointment_id}").status_code == 404

    replacement = admin.post(
        "/agendamentos/novo",
        data=dict(VALID_APPOINTMENT, patient_name="Paciente Substituto Silva",
                  csrf_token=csrf_from(admin.get("/agendamentos"))),
    )
    assert replacement.status_code == 302 and "/agendamentos/" in replacement.location


def test_privacy_page_is_clear_for_public_and_admin_visitors():
    public = application.app.test_client().get("/privacidade")
    public_text = public.get_data(as_text=True)
    assert public.status_code == 200
    assert "Uso responsável" in public_text and "Exclusão de agendamentos" in public_text
    admin = application.app.test_client()
    login(admin)
    admin_text = admin.get("/privacidade").get_data(as_text=True)
    assert "Voltar ao sistema" in admin_text and "Navegação administrativa" in admin_text
