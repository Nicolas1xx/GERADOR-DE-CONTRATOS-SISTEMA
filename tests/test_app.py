import base64
import hashlib
import json
import os
import re
import zlib
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
PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(
    base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
).decode()


@pytest.fixture(autouse=True)
def clean_database():
    with application.store.connect() as connection:
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
