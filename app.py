import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import unicodedata
import uuid
import zlib
from calendar import Calendar
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from flask import (Flask, abort, flash, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

from contract_generator import PLACEHOLDERS, default_contract_form, generate_contract_docx
from storage import AppointmentConflictError, ContractStore


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE = os.path.join(BASE_DIR, "modelo_contrato_odontologico.docx")
DEFAULT_LOGO = os.path.join(BASE_DIR, "logo_consultorio_angelo.jpg")
LINK_TTL_SECONDS = 7 * 24 * 60 * 60
BRAZIL_TZ = ZoneInfo("America/Sao_Paulo")
FORM_FIELDS = sorted(set(PLACEHOLDERS.values()) | {"data_contrato"})
FIELD_CODES = {field: format(index, "x") for index, field in enumerate(FORM_FIELDS)}
CODE_FIELDS = {code: field for field, code in FIELD_CODES.items()}
FIELD_LIMITS = {field: 180 for field in FORM_FIELDS} | {
    "email": 150, "procedimentos": 1500, "complemento": 80, "numero": 10,
    "cpf_contratante": 14, "cpf_cnpj_contratada": 18, "cep_contratante": 9,
    "telefone": 19, "rg_contratante": 20, "cro": 18, "data_contrato": 10,
    "valor_total": 25, "valor_avaliacao": 25, "limite_atraso": 3,
}
STATUS_LABELS = {
    "AGUARDANDO": "Aguardando envio", "ENVIADO": "Enviado",
    "VISUALIZADO": "Visualizado", "ASSINADO": "Assinado", "EXPIRADO": "Expirado",
}
EVENT_LABELS = {
    "CRIADO": "Contrato criado", "ENVIADO": "Link enviado",
    "VISUALIZADO": "Paciente visualizou", "ASSINADO": "Contrato assinado",
    "EXPIRADO": "Contrato expirado", "EXCLUIDO": "Contrato excluído da área administrativa",
}
APPOINTMENT_STATUS_LABELS = {
    "AGENDADO": "Agendado", "CONFIRMADO": "Confirmado", "AGUARDANDO": "Aguardando",
    "EM_ATENDIMENTO": "Em atendimento", "CONCLUIDO": "Concluído",
    "CANCELADO": "Cancelado", "FALTOU": "Faltou",
}
APPOINTMENT_EVENT_LABELS = {
    "CRIADO": "Agendamento criado", "ATUALIZADO": "Dados atualizados",
    "HORARIO_ALTERADO": "Horário alterado", "STATUS_ALTERADO": "Status alterado",
    "CONFIRMADO": "Agendamento confirmado", "CONCLUIDO": "Atendimento concluído",
    "CANCELADO": "Agendamento cancelado", "CONFIRMACAO_ENVIADA": "Confirmação preparada",
    "EXCLUIDO": "Agendamento excluído",
}
APPOINTMENT_FIELD_LIMITS = {
    "patient_name": 120, "cpf": 14, "phone": 19, "email": 150,
    "professional": 120, "observations": 1500,
}
ADMIN_DISPLAY_NAME = os.environ.get("ADMIN_DISPLAY_NAME", "Dr. Ângelo G. Martinez").strip() or "Dr. Ângelo G. Martinez"


def _secret(name, development_fallback):
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if os.environ.get("VERCEL"):
        raise RuntimeError(f"A variável obrigatória {name} não foi configurada.")
    return development_fallback


SESSION_SECRET = _secret("SECRET_KEY", "desenvolvimento-sessao-altere-em-producao")
DATA_SECRET = _secret("DATA_ENCRYPTION_SECRET", "desenvolvimento-dados-altere-em-producao")
RATE_SECRET = _secret("RATE_LIMIT_SECRET", "desenvolvimento-rate-limit")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = SESSION_SECRET
app.config.update(
    MAX_CONTENT_LENGTH=4 * 1024 * 1024,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL")),
)
store = ContractStore()


def _key_from_secret(value):
    return base64.urlsafe_b64encode(hashlib.sha256(value.encode("utf-8")).digest())


data_cipher = Fernet(_key_from_secret(DATA_SECRET))


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_iso(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _display_date(value, include_time=False):
    local = _parse_iso(value).astimezone(BRAZIL_TZ)
    return local.strftime("%d/%m/%Y às %H:%M" if include_time else "%d/%m/%Y")


def _display_optional(value, include_time=True):
    return _display_date(value, include_time) if value else "—"


def _masked_cpf(value):
    digits = re.sub(r"\D", "", value or "")
    return f"***.***.***-{digits[-2:]}" if len(digits) == 11 else "—"


def _audit_actor():
    return ADMIN_DISPLAY_NAME


def _display_actor(actor):
    technical_names = {"admin", "admin-consultorio", _admin_username().casefold()}
    return ADMIN_DISPLAY_NAME if (actor or "").casefold() in technical_names else (actor or ADMIN_DISPLAY_NAME)


def _tracking_identity(row):
    try:
        form = _decrypt_form(row["data_ciphertext"])
        return form.get("nome_paciente") or "Paciente não informado", _masked_cpf(form.get("cpf_contratante"))
    except (InvalidToken, ValueError, json.JSONDecodeError):
        return "Dados indisponíveis", "—"


def _valid_cpf(value):
    numbers = re.sub(r"\D", "", value or "")
    if len(numbers) != 11 or numbers == numbers[0] * 11:
        return False
    for size in (9, 10):
        total = sum(int(numbers[index]) * (size + 1 - index) for index in range(size))
        if (total * 10 % 11) % 10 != int(numbers[size]):
            return False
    return True


def _valid_cnpj(value):
    numbers = re.sub(r"\D", "", value or "")
    if len(numbers) != 14 or numbers == numbers[0] * 14:
        return False
    checks = ((12, [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]),
              (13, [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]))
    for size, weights in checks:
        remainder = sum(int(numbers[index]) * weights[index] for index in range(size)) % 11
        if (0 if remainder < 2 else 11 - remainder) != int(numbers[size]):
            return False
    return True


def _valid_money(value):
    return bool(re.fullmatch(r"(?:R\$\s*)?(?:\d{1,3}(?:\.\d{3})*|\d+),\d{2}", (value or "").strip()))


def _normalize_phone(value):
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("55") and len(digits) in (12, 13):
        digits = digits[2:]
    if len(digits) not in (10, 11):
        return None
    ddd, number = digits[:2], digits[2:]
    if ddd[0] == "0" or digits == digits[0] * len(digits):
        return None
    if len(number) == 9 and number[0] != "9":
        return None
    if len(number) == 8 and number[0] not in "2345":
        return None
    return "55" + digits


def _format_phone(value):
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("55") and len(digits) in (12, 13):
        digits = digits[2:]
    if len(digits) == 11:
        return f"({digits[:2]}) {digits[2:7]}-{digits[7:]}"
    if len(digits) == 10:
        return f"({digits[:2]}) {digits[2:6]}-{digits[6:]}"
    return value or ""


def _validate(form):
    required = {
        "nome_contratante": "nome do contratante", "cpf_contratante": "CPF do contratante",
        "rg_contratante": "RG do contratante", "cidade_contratante": "cidade do contratante",
        "endereco_contratante": "endereço do contratante", "numero": "número do endereço",
        "bairro_contratante": "bairro", "cep_contratante": "CEP", "telefone": "telefone do paciente",
        "email": "e-mail", "nome_contratada": "nome da clínica/contratada",
        "cpf_cnpj_contratada": "CPF/CNPJ da contratada", "cro": "CRO da contratada",
        "endereco_clinica": "endereço profissional da clínica", "nome_paciente": "nome do paciente",
        "procedimentos": "procedimentos", "valor_total": "valor total do tratamento",
        "forma_pagamento": "forma de pagamento", "parcelas": "número de parcelas",
        "vencimentos": "vencimentos", "valor_avaliacao": "valor da avaliação",
        "limite_atraso": "limite de atraso", "cidade_clinica": "cidade da clínica",
        "data_contrato": "data do contrato",
    }
    errors = {}
    for field, label in required.items():
        if not form.get(field, "").strip():
            errors[field] = f"Informe {label}."
    for field, limit in FIELD_LIMITS.items():
        if len(form.get(field, "").strip()) > limit:
            errors[field] = f"Este campo deve ter no máximo {limit} caracteres."
    if form.get("cpf_contratante") and not _valid_cpf(form.get("cpf_contratante")):
        errors["cpf_contratante"] = "Informe um CPF válido."
    clinic_document = re.sub(r"\D", "", form.get("cpf_cnpj_contratada", ""))
    if clinic_document and not (_valid_cpf(clinic_document) if len(clinic_document) == 11 else _valid_cnpj(clinic_document)):
        errors["cpf_cnpj_contratada"] = "Informe um CPF ou CNPJ válido para a contratada."
    if form.get("cep_contratante") and len(re.sub(r"\D", "", form.get("cep_contratante", ""))) != 8:
        errors["cep_contratante"] = "Informe um CEP válido com 8 números."
    if form.get("telefone") and not _normalize_phone(form.get("telefone")):
        errors["telefone"] = "Informe um telefone válido com DDD."
    rg_digits = re.sub(r"\W", "", form.get("rg_contratante", ""))
    if form.get("rg_contratante") and not 7 <= len(rg_digits) <= 14:
        errors["rg_contratante"] = "Informe um RG válido."
    if form.get("email") and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", form.get("email", "").strip()):
        errors["email"] = "Informe um e-mail válido."
    for field, label in (("valor_total", "valor total"), ("valor_avaliacao", "valor da avaliação")):
        if form.get(field) and not _valid_money(form.get(field)):
            errors[field] = f"Informe o {label} no formato 0,00."
    try:
        if not 1 <= int(form.get("limite_atraso", "0")) <= 180:
            raise ValueError
    except ValueError:
        errors["limite_atraso"] = "Informe um limite entre 1 e 180 minutos."
    if form.get("procedimentos") and len(form.get("procedimentos", "").strip()) < 3:
        errors["procedimentos"] = "Descreva os procedimentos odontológicos."
    for field, label in (("nome_contratante", "nome completo do contratante"), ("nome_paciente", "nome completo do paciente")):
        value = form.get(field, "").strip()
        if value and (not re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ' -]{5,120}", value) or " " not in value):
            errors[field] = f"Informe o {label}."
    if form.get("numero") and not re.fullmatch(r"(?:\d{1,8}[A-Za-z]?|S/?N)", form.get("numero", "").strip(), re.I):
        errors["numero"] = "Informe um número de endereço válido ou S/N."
    if form.get("cro") and not re.fullmatch(r"CRO-[A-Z]{2}\s\d{3,8}", form.get("cro", "").strip().upper()):
        errors["cro"] = "Informe o CRO no formato CRO-UF 12345."
    for field, label in (("cidade_contratante", "cidade do contratante"), ("cidade_clinica", "cidade da clínica")):
        city = form.get(field, "").strip()
        parts = city.rsplit("/", 1)
        if city and (len(parts) != 2 or len(parts[0].strip()) < 2 or not re.fullmatch(r"[A-Za-z]{2}", parts[1])):
            errors[field] = f"Informe a {label} no formato Cidade/UF."
    try:
        datetime.strptime(form.get("data_contrato", ""), "%d/%m/%Y")
    except ValueError:
        errors["data_contrato"] = "Informe uma data válida no formato DD/MM/AAAA."
    return errors


def _safe_form(form):
    cleaned = {field: form.get(field, "").strip()[:FIELD_LIMITS[field]] for field in FORM_FIELDS}
    cleaned["cro"] = cleaned["cro"].upper()
    cleaned["telefone"] = _format_phone(cleaned["telefone"])
    return cleaned


def _download(form, signed=False):
    with open(DEFAULT_LOGO, "rb") as logo_file:
        arquivo = generate_contract_docx(form, DEFAULT_TEMPLATE, logo_file)
    nome = re.sub(r"[^A-Za-z0-9_-]+", "_", form["nome_paciente"].strip()).strip("_") or "paciente"
    suffix = "_assinado" if signed else ""
    return send_file(arquivo, as_attachment=True, download_name=f"contrato_{nome}{suffix}.docx",
                     mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


def _csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def _check_csrf():
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    if not hmac.compare_digest(supplied, session.get("csrf_token", "")):
        abort(400, description="Sua sessão de segurança expirou. Atualize a página e tente novamente.")


def _identity_hash():
    forwarded = request.headers.get("X-Forwarded-For", request.remote_addr or "desconhecido")
    ip = forwarded.split(",", 1)[0].strip()
    return hmac.new(RATE_SECRET.encode(), ip.encode(), hashlib.sha256).hexdigest()


def _rate_limit(bucket, limit, window):
    if not store.allow_request(bucket, _identity_hash(), limit, window):
        return jsonify({"erro": "Muitas tentativas em pouco tempo. Aguarde e tente novamente."}), 429
    return None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_user"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def _encrypt_form(form):
    raw = json.dumps(form, ensure_ascii=False, separators=(",", ":")).encode()
    return data_cipher.encrypt(raw).decode("ascii")


def _decrypt_form(ciphertext):
    raw = data_cipher.decrypt(ciphertext.encode("ascii"))
    return _safe_form(json.loads(raw.decode("utf-8")))


def _admin_username():
    return os.environ.get("ADMIN_USERNAME", "admin").strip()


def _password_is_valid(password):
    password_hash = os.environ.get("ADMIN_PASSWORD_HASH", "")
    return bool(password_hash) and check_password_hash(password_hash, password)


def _tracking_item(row, include_events=False):
    patient, cpf = _tracking_identity(row)
    events = store.events_for(row["id"])
    sent_at = next((event["created_at"] for event in events if event["event_type"] == "ENVIADO"), None)
    item = {
        "id": row["id"], "number": row["contract_number"], "patient": patient, "cpf": cpf,
        "status": row["status"], "created_by": _display_actor(row["created_by"]),
        "created_at": _display_date(row["created_at"], True),
        "expires_at": _display_date(row["expires_at"], True),
        "sent_at": _display_optional(sent_at),
        "viewed_at": _display_optional(row.get("first_viewed_at")),
        "signed_at": _display_optional(row.get("signed_at")),
        "is_signed": bool(row.get("signed_at")), "is_expired": row["status"] == "EXPIRADO",
    }
    if include_events:
        item["events"] = [{**event, "actor": _display_actor(event["actor"]),
                           "label": EVENT_LABELS.get(event["event_type"], event["event_type"]),
                           "display_date": _display_date(event["created_at"], True)} for event in events]
    return item


def _filter_date(value, end=False):
    if not value:
        return None
    parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=BRAZIL_TZ)
    if end:
        parsed += timedelta(days=1)
    return _iso(parsed.astimezone(timezone.utc))


def _normalize_search(value):
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_text = "".join(character for character in normalized if not unicodedata.combining(character))
    return " ".join(ascii_text.casefold().split())


def _appointment_blind_token(kind, value):
    message = f"appointment:{kind}:{value}".encode("utf-8")
    return hmac.new(DATA_SECRET.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _professional_key(value):
    return _appointment_blind_token("professional", _normalize_search(value))


def _appointment_search_tokens(data):
    tokens = {_appointment_blind_token("name", word)
              for word in _normalize_search(data.get("patient_name", "")).split() if len(word) > 1}
    for field in ("cpf", "phone"):
        digits = re.sub(r"\D", "", data.get(field, ""))
        if digits:
            tokens.add(_appointment_blind_token("digits", digits))
    return tokens


def _appointment_query_tokens(value):
    digits = re.sub(r"\D", "", value or "")
    if len(digits) >= 8:
        return [_appointment_blind_token("digits", digits)]
    words = [word for word in _normalize_search(value).split() if len(word) > 1]
    return [_appointment_blind_token("name", word) for word in words]


def _encrypt_appointment(data):
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return data_cipher.encrypt(raw).decode("ascii")


def _decrypt_appointment(ciphertext):
    raw = data_cipher.decrypt(ciphertext.encode("ascii"))
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Dados de agendamento inválidos")
    return data


def _validate_appointment(form):
    raw_fields = {key: (form.get(key, "") or "").strip()
                  for key in APPOINTMENT_FIELD_LIMITS}
    fields = {key: value[:APPOINTMENT_FIELD_LIMITS[key]] for key, value in raw_fields.items()}
    fields.update({
        "appointment_date": (form.get("appointment_date", "") or "").strip(),
        "appointment_time": (form.get("appointment_time", "") or "").strip(),
        "appointment_type_id": (form.get("appointment_type_id", "") or "").strip(),
        "status": (form.get("status", "AGENDADO") or "").strip().upper(),
    })
    errors = {}
    for key, value in raw_fields.items():
        if len(value) > APPOINTMENT_FIELD_LIMITS[key]:
            errors[key] = f"Este campo deve ter no máximo {APPOINTMENT_FIELD_LIMITS[key]} caracteres."
    required = {"patient_name": "nome completo do paciente", "cpf": "CPF", "phone": "telefone",
                "appointment_date": "data do atendimento", "appointment_time": "horário",
                "appointment_type_id": "tipo de atendimento", "professional": "profissional responsável",
                "status": "status"}
    for key, label in required.items():
        if not fields.get(key):
            errors[key] = f"Informe {label}."
    name = fields["patient_name"]
    if name and (not re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ' -]{5,120}", name) or " " not in name):
        errors["patient_name"] = "Informe o nome completo do paciente."
    if fields["cpf"] and not _valid_cpf(fields["cpf"]):
        errors["cpf"] = "Informe um CPF válido."
    if fields["phone"] and not _normalize_phone(fields["phone"]):
        errors["phone"] = "Informe um telefone válido com DDD."
    if fields["email"] and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", fields["email"]):
        errors["email"] = "Informe um e-mail válido."
    try:
        date.fromisoformat(fields["appointment_date"])
    except ValueError:
        errors["appointment_date"] = "Informe uma data válida."
    try:
        datetime.strptime(fields["appointment_time"], "%H:%M")
    except ValueError:
        errors["appointment_time"] = "Informe um horário válido."
    type_ids = {item["id"] for item in store.appointment_types()}
    if fields["appointment_type_id"] and fields["appointment_type_id"] not in type_ids:
        errors["appointment_type_id"] = "Selecione um tipo de atendimento válido."
    if fields["status"] not in APPOINTMENT_STATUS_LABELS:
        errors["status"] = "Selecione um status válido."
    if fields["professional"] and not re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ.' -]{3,120}", fields["professional"]):
        errors["professional"] = "Informe o profissional responsável."
    fields["cpf"] = re.sub(r"\D", "", fields["cpf"])
    fields["phone"] = _format_phone(fields["phone"])
    return fields, errors


def _appointment_item(row, include_events=False):
    try:
        data = _decrypt_appointment(row["data_ciphertext"])
    except (InvalidToken, ValueError, json.JSONDecodeError):
        data = {"patient_name": "Dados indisponíveis", "cpf": "", "phone": "", "email": "",
                "observations": ""}
    appointment_date = date.fromisoformat(row["appointment_date"])
    item = {
        **data, "id": row["id"], "appointment_date": row["appointment_date"],
        "appointment_date_display": appointment_date.strftime("%d/%m/%Y"),
        "appointment_time": row["appointment_time"], "appointment_type_id": row["appointment_type_id"],
        "appointment_type_name": row["appointment_type_name"], "professional": row["professional"],
        "status": row["status"], "status_label": APPOINTMENT_STATUS_LABELS[row["status"]],
        "created_by": _display_actor(row["created_by"]), "created_at": _display_date(row["created_at"], True),
        "updated_at": _display_date(row["updated_at"], True), "phone_whatsapp": _normalize_phone(data.get("phone")),
        "cpf_masked": _masked_cpf(data.get("cpf")),
    }
    if include_events:
        item["events"] = [{**event, "actor": _display_actor(event["actor"]),
                           "label": APPOINTMENT_EVENT_LABELS.get(event["event_type"], event["event_type"]),
                           "display_date": _display_date(event["created_at"], True)}
                          for event in store.appointment_events(row["id"])]
    return item


def _agenda_bounds(view, reference):
    if view == "day":
        return reference, reference + timedelta(days=1)
    if view == "week":
        start = reference - timedelta(days=reference.weekday())
        return start, start + timedelta(days=7)
    if view == "month":
        start = reference.replace(day=1)
        next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return start, next_month
    return reference, reference + timedelta(days=1)


def _appointment_update_events(old_row, old_data, new_data, actor, timestamp):
    events = []
    old_date, old_time = old_row["appointment_date"], old_row["appointment_time"]
    if (old_date, old_time) != (new_data["appointment_date"], new_data["appointment_time"]):
        before = f"{date.fromisoformat(old_date):%d/%m/%Y} às {old_time}"
        after = f"{date.fromisoformat(new_data['appointment_date']):%d/%m/%Y} às {new_data['appointment_time']}"
        events.append({"event_type": "HORARIO_ALTERADO", "actor": actor,
                       "description": f"Horário alterado de {before} para {after}", "created_at": timestamp})
    if old_row["status"] != new_data["status"]:
        event_type = {"CONFIRMADO": "CONFIRMADO", "CONCLUIDO": "CONCLUIDO",
                      "CANCELADO": "CANCELADO"}.get(new_data["status"], "STATUS_ALTERADO")
        description = f"Status alterado de {APPOINTMENT_STATUS_LABELS[old_row['status']]} para {APPOINTMENT_STATUS_LABELS[new_data['status']]}"
        events.append({"event_type": event_type, "actor": actor,
                       "description": description, "created_at": timestamp})
    comparison_fields = ("patient_name", "cpf", "phone", "email", "professional",
                         "observations", "appointment_type_id")
    if any(old_data.get(field, "") != new_data.get(field, "") for field in comparison_fields):
        events.append({"event_type": "ATUALIZADO", "actor": actor,
                       "description": "Dados do agendamento atualizados", "created_at": timestamp})
    if not events:
        events = [{"event_type": "ATUALIZADO", "actor": actor,
                   "description": "Agendamento salvo sem alteração de dados", "created_at": timestamp}]
    base_time = _parse_iso(timestamp)
    for position, event in enumerate(events):
        event["created_at"] = _iso(base_time + timedelta(microseconds=position))
    return events


@app.context_processor
def template_helpers():
    return {"csrf_token": _csrf_token, "status_labels": STATUS_LABELS, "event_labels": EVENT_LABELS,
            "appointment_status_labels": APPOINTMENT_STATUS_LABELS}


@app.after_request
def security_headers(response):
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if session.get("admin_user") or request.path.startswith(("/assinar/", "/api/", "/login", "/logout", "/gerar")):
        response.headers["Cache-Control"] = "no-store, private"
    return response


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("admin_user"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        limited = _rate_limit("login", 8, 15 * 60)
        if limited:
            return limited
        _check_csrf()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if hmac.compare_digest(username, _admin_username()) and _password_is_valid(password):
            session.clear()
            session.permanent = True
            session["admin_user"] = _admin_username()
            session["csrf_token"] = secrets.token_urlsafe(32)
            return redirect(url_for("index"))
        error = "Usuário ou senha incorretos."
    return render_template("login.html", error=error), (401 if error else 200)


@app.post("/logout")
@login_required
def logout():
    _check_csrf()
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def index():
    return render_template("index.html", valores=default_contract_form())


@app.get("/acompanhamento")
@login_required
def tracking():
    store.expire_due(_iso(_now()))
    query = request.args.get("q", "").strip()[:120]
    status = request.args.get("status", "").strip().upper()
    date_from = request.args.get("data_de", "").strip()
    date_to = request.args.get("data_ate", "").strip()
    if status not in STATUS_LABELS:
        status = ""
    filter_error = None
    try:
        created_from = _filter_date(date_from)
        created_until = _filter_date(date_to, end=True)
        if created_from and created_until and created_from >= created_until:
            raise ValueError
    except ValueError:
        created_from = created_until = None
        filter_error = "Informe um período válido para a pesquisa."
    rows = store.list_filtered(status or None, created_from, created_until)
    normalized_query = query.casefold()
    filtered_rows = []
    for row in rows:
        patient, _ = _tracking_identity(row)
        if normalized_query and normalized_query not in patient.casefold() \
                and normalized_query not in row["contract_number"].casefold():
            continue
        filtered_rows.append(row)
    try:
        page = max(1, int(request.args.get("pagina", "1")))
    except ValueError:
        page = 1
    per_page = 20
    total = len(filtered_rows)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    page_rows = filtered_rows[(page - 1) * per_page:page * per_page]
    contracts = [_tracking_item(row) for row in page_rows]
    base_params = {"q": query, "status": status, "data_de": date_from, "data_ate": date_to}
    previous_url = url_for("tracking", **base_params, pagina=page - 1) if page > 1 else None
    next_url = url_for("tracking", **base_params, pagina=page + 1) if page < total_pages else None
    return render_template("acompanhamento.html", contracts=contracts, total=total, page=page,
                           total_pages=total_pages, previous_url=previous_url, next_url=next_url,
                           filters=base_params, filter_error=filter_error)


@app.get("/acompanhamento/<contract_id>")
@login_required
def contract_history(contract_id):
    try:
        uuid.UUID(contract_id)
    except ValueError:
        abort(404)
    store.expire_due(_iso(_now()))
    row = store.get_by_id(contract_id)
    if not row:
        abort(404)
    return render_template("contract_detail.html", contract=_tracking_item(row, include_events=True))


@app.post("/acompanhamento/<contract_id>/excluir")
@login_required
def delete_contract(contract_id):
    limited = _rate_limit("delete-contract", 30, 60 * 60)
    if limited:
        return limited
    _check_csrf()
    try:
        uuid.UUID(contract_id)
    except ValueError:
        abort(404)
    if not store.get_by_id(contract_id):
        abort(404)
    if not store.soft_delete_contract(contract_id, _audit_actor(), _iso(_now())):
        flash("Não foi possível excluir o contrato. Atualize a página e tente novamente.", "erro")
        return redirect(url_for("tracking"))
    flash("Contrato excluído da lista e link de assinatura revogado. Os dados e o histórico foram preservados.", "sucesso")
    return redirect(url_for("tracking"))


@app.get("/agendamentos")
@login_required
def appointments():
    today = _now().astimezone(BRAZIL_TZ).date()
    view = request.args.get("visao", "month").strip().lower()
    if view not in {"day", "week", "month", "list"}:
        view = "month"
    try:
        reference = date.fromisoformat(request.args.get("data", ""))
    except ValueError:
        reference = today
    period = request.args.get("periodo", "").strip().lower()
    start, end = _agenda_bounds("day" if view == "list" else view, reference)
    if period == "today":
        start, end = today, today + timedelta(days=1)
    elif period == "tomorrow":
        start, end = today + timedelta(days=1), today + timedelta(days=2)
    elif period == "week":
        start, end = _agenda_bounds("week", today)
    elif period == "month":
        start, end = _agenda_bounds("month", today)
    elif period == "custom":
        try:
            start = date.fromisoformat(request.args.get("data_de", ""))
            custom_end = date.fromisoformat(request.args.get("data_ate", ""))
            if custom_end < start:
                raise ValueError
            end = custom_end + timedelta(days=1)
        except ValueError:
            flash("Informe um período personalizado válido.", "erro")
            start, end = _agenda_bounds("day" if view == "list" else view, reference)
    status = request.args.get("status", "").strip().upper()
    if status not in APPOINTMENT_STATUS_LABELS:
        status = ""
    type_id = request.args.get("tipo", "").strip()
    type_ids = {item["id"] for item in store.appointment_types()}
    if type_id not in type_ids:
        type_id = ""
    professional_name = request.args.get("profissional", "").strip()[:120]
    professional = _professional_key(professional_name) if professional_name else None
    query = request.args.get("q", "").strip()[:120]
    query_tokens = _appointment_query_tokens(query) if query else None
    rows = store.list_appointments(start.isoformat(), end.isoformat(), status or None,
                                   professional, type_id or None, query_tokens)
    items = [_appointment_item(row) for row in rows]

    today_rows = store.list_appointments(today.isoformat(), (today + timedelta(days=1)).isoformat())
    today_items = [_appointment_item(row) for row in today_rows]
    indicators = {
        "today": len(today_items),
        "confirmed": sum(item["status"] == "CONFIRMADO" for item in today_items),
        "waiting": sum(item["status"] == "AGUARDANDO" for item in today_items),
        "completed": sum(item["status"] == "CONCLUIDO" for item in today_items),
    }
    by_date = {}
    for item in items:
        by_date.setdefault(item["appointment_date"], []).append(item)
    calendar_weeks = []
    if view == "month":
        for week in Calendar(firstweekday=0).monthdatescalendar(reference.year, reference.month):
            calendar_weeks.append([{"date": day, "date_iso": day.isoformat(),
                                    "in_month": day.month == reference.month,
                                    "is_today": day == today, "appointments": by_date.get(day.isoformat(), [])}
                                   for day in week])
    week_days = []
    if view == "week":
        week_start, _ = _agenda_bounds("week", reference)
        week_days = [{"date": week_start + timedelta(days=index),
                      "date_iso": (week_start + timedelta(days=index)).isoformat(),
                      "appointments": by_date.get((week_start + timedelta(days=index)).isoformat(), [])}
                     for index in range(7)]
    if view == "month":
        previous_reference = (reference.replace(day=1) - timedelta(days=1)).replace(day=1)
        next_reference = (reference.replace(day=28) + timedelta(days=4)).replace(day=1)
        period_label = reference.strftime("%m/%Y")
    elif view == "week":
        previous_reference, next_reference = reference - timedelta(days=7), reference + timedelta(days=7)
        period_label = f"{start:%d/%m} a {(end - timedelta(days=1)):%d/%m/%Y}"
    else:
        previous_reference, next_reference = reference - timedelta(days=1), reference + timedelta(days=1)
        period_label = reference.strftime("%d/%m/%Y")
    nav_params = request.args.to_dict()
    nav_params.pop("periodo", None)
    nav_params.pop("data_de", None)
    nav_params.pop("data_ate", None)
    previous_url = url_for("appointments", **{**nav_params, "visao": view, "data": previous_reference.isoformat()})
    next_url = url_for("appointments", **{**nav_params, "visao": view, "data": next_reference.isoformat()})
    today_url = url_for("appointments", **{**nav_params, "visao": view, "data": today.isoformat()})
    return render_template("appointments.html", appointments=items, today_appointments=today_items,
                           indicators=indicators, view=view, reference=reference, period_label=period_label,
                           calendar_weeks=calendar_weeks, week_days=week_days, today=today,
                           appointment_types=store.appointment_types(), professionals=store.appointment_professionals(),
                           previous_url=previous_url, next_url=next_url, today_url=today_url,
                           filters={"q": query, "status": status, "tipo": type_id,
                                    "profissional": professional_name, "periodo": period,
                                    "data_de": request.args.get("data_de", ""),
                                    "data_ate": request.args.get("data_ate", "")},
                           default_appointment={"appointment_date": today.isoformat(), "appointment_time": "09:00",
                                                "professional": "Dr. Ângelo G. Martinez", "status": "AGENDADO"})


@app.post("/agendamentos/novo")
@login_required
def create_appointment():
    limited = _rate_limit("create-appointment", 60, 60 * 60)
    if limited:
        return limited
    _check_csrf()
    data, errors = _validate_appointment(request.form)
    if errors:
        flash(next(iter(errors.values())), "erro")
        return redirect(url_for("appointments"))
    now = _iso(_now())
    appointment_id = str(uuid.uuid4())
    record = {
        "id": appointment_id, "data_ciphertext": _encrypt_appointment(data),
        "appointment_date": data["appointment_date"], "appointment_time": data["appointment_time"],
        "appointment_type_id": data["appointment_type_id"], "professional": data["professional"],
        "professional_key": _professional_key(data["professional"]), "status": data["status"],
        "created_by": _audit_actor(), "created_at": now, "updated_at": now, "contract_id": None,
    }
    try:
        store.create_appointment(record, _appointment_search_tokens(data))
    except AppointmentConflictError:
        flash("Este horário já possui um atendimento agendado para este profissional. Escolha outro horário.", "erro")
        return redirect(url_for("appointments", visao="day", data=data["appointment_date"]))
    flash("Agendamento criado com sucesso.", "sucesso")
    return redirect(url_for("appointment_detail", appointment_id=appointment_id))


@app.get("/agendamentos/<appointment_id>")
@login_required
def appointment_detail(appointment_id):
    try:
        uuid.UUID(appointment_id)
    except ValueError:
        abort(404)
    row = store.get_appointment(appointment_id)
    if not row:
        abort(404)
    return render_template("appointment_detail.html", appointment=_appointment_item(row, include_events=True),
                           appointment_types=store.appointment_types())


@app.post("/agendamentos/<appointment_id>/editar")
@login_required
def edit_appointment(appointment_id):
    limited = _rate_limit("edit-appointment", 120, 60 * 60)
    if limited:
        return limited
    _check_csrf()
    row = store.get_appointment(appointment_id)
    if not row:
        abort(404)
    data, errors = _validate_appointment(request.form)
    if errors:
        flash(next(iter(errors.values())), "erro")
        return redirect(url_for("appointment_detail", appointment_id=appointment_id))
    try:
        old_data = _decrypt_appointment(row["data_ciphertext"])
    except (InvalidToken, ValueError, json.JSONDecodeError):
        abort(400, description="Os dados deste agendamento não puderam ser lidos.")
    timestamp = _iso(_now())
    record = {"data_ciphertext": _encrypt_appointment(data), "appointment_date": data["appointment_date"],
              "appointment_time": data["appointment_time"], "appointment_type_id": data["appointment_type_id"],
              "professional": data["professional"], "professional_key": _professional_key(data["professional"]),
              "status": data["status"], "updated_at": timestamp}
    events = _appointment_update_events(row, old_data, data, _audit_actor(), timestamp)
    try:
        store.update_appointment(appointment_id, record, _appointment_search_tokens(data), events)
    except AppointmentConflictError:
        flash("Este horário já possui um atendimento agendado para este profissional. Escolha outro horário.", "erro")
        return redirect(url_for("appointment_detail", appointment_id=appointment_id))
    flash("Agendamento atualizado com sucesso.", "sucesso")
    return redirect(url_for("appointment_detail", appointment_id=appointment_id))


@app.post("/agendamentos/<appointment_id>/status")
@login_required
def update_appointment_status(appointment_id):
    limited = _rate_limit("appointment-status", 120, 60 * 60)
    if limited:
        return limited
    _check_csrf()
    row = store.get_appointment(appointment_id)
    if not row:
        abort(404)
    new_status = request.form.get("status", "").strip().upper()
    if new_status not in APPOINTMENT_STATUS_LABELS:
        abort(400, description="Status de agendamento inválido.")
    if row["status"] == new_status:
        return redirect(url_for("appointment_detail", appointment_id=appointment_id))
    data = _decrypt_appointment(row["data_ciphertext"])
    data["status"] = new_status
    timestamp = _iso(_now())
    record = {"data_ciphertext": _encrypt_appointment(data), "appointment_date": row["appointment_date"],
              "appointment_time": row["appointment_time"], "appointment_type_id": row["appointment_type_id"],
              "professional": row["professional"], "professional_key": row["professional_key"],
              "status": new_status, "updated_at": timestamp}
    events = _appointment_update_events(row, _decrypt_appointment(row["data_ciphertext"]), data,
                                        _audit_actor(), timestamp)
    try:
        store.update_appointment(appointment_id, record, _appointment_search_tokens(data), events)
    except AppointmentConflictError:
        flash("Não é possível reativar este agendamento porque o horário já está ocupado.", "erro")
        return redirect(url_for("appointment_detail", appointment_id=appointment_id))
    flash(f"Status alterado para {APPOINTMENT_STATUS_LABELS[new_status]}.", "sucesso")
    return redirect(url_for("appointment_detail", appointment_id=appointment_id))


@app.post("/api/appointments/<appointment_id>/confirmation-sent")
@login_required
def appointment_confirmation_sent(appointment_id):
    limited = _rate_limit("appointment-whatsapp", 120, 60 * 60)
    if limited:
        return limited
    _check_csrf()
    if not store.add_appointment_event(appointment_id, "CONFIRMACAO_ENVIADA", _audit_actor(),
                                       "Confirmação preparada para envio pelo WhatsApp", _iso(_now())):
        return jsonify({"erro": "Agendamento não encontrado."}), 404
    return jsonify({"ok": True})


@app.post("/agendamentos/<appointment_id>/excluir")
@login_required
def delete_appointment(appointment_id):
    limited = _rate_limit("delete-appointment", 30, 60 * 60)
    if limited:
        return limited
    _check_csrf()
    row = store.get_appointment(appointment_id)
    if not row:
        abort(404)
    if not store.soft_delete_appointment(appointment_id, _audit_actor(), _iso(_now())):
        flash("Não foi possível excluir o agendamento. Atualize a página e tente novamente.", "erro")
        return redirect(url_for("appointment_detail", appointment_id=appointment_id))
    flash("Agendamento excluído da agenda. O histórico foi preservado com segurança.", "sucesso")
    return redirect(url_for("appointments"))


@app.get("/logo")
def logo():
    return send_file(DEFAULT_LOGO, mimetype="image/jpeg", max_age=86400)


@app.post("/gerar")
@login_required
def gerar():
    _check_csrf()
    errors = _validate(request.form)
    if errors:
        flash("Revise os campos destacados antes de continuar.", "erro")
        return render_template("index.html", valores=request.form, field_errors=errors), 400
    try:
        return _download(_safe_form(request.form))
    except Exception:
        app.logger.exception("Falha ao gerar Word sem assinatura")
        flash("Não foi possível gerar o arquivo Word. Tente novamente.", "erro")
        return render_template("index.html", valores=request.form), 500


@app.post("/api/contracts")
@login_required
def create_contract():
    limited = _rate_limit("create-contract", 30, 60 * 60)
    if limited:
        return limited
    _check_csrf()
    errors = _validate(request.form)
    if errors:
        return jsonify({"erro": "Revise os campos informados.", "campos": errors}), 400
    form = _safe_form(request.form)
    token = secrets.token_urlsafe(32)
    now = _now()
    expires = now + timedelta(seconds=LINK_TTL_SECONDS)
    contract_id = str(uuid.uuid4())
    contract_number = f"CTR-{now.astimezone(BRAZIL_TZ):%Y%m%d}-{secrets.token_hex(3).upper()}"
    store.create_contract({
        "id": contract_id, "contract_number": contract_number,
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "data_ciphertext": _encrypt_form(form), "created_by": _audit_actor(),
        "created_at": _iso(now), "expires_at": _iso(expires), "status": "AGUARDANDO",
        "first_viewed_at": None, "signed_at": None,
    })
    configured_base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if configured_base:
        public_url = f"{configured_base}/assinar/{token}"
    else:
        scheme = "https" if os.environ.get("VERCEL") else request.scheme
        public_url = url_for("sign_contract", token=token, _external=True, _scheme=scheme)
    return jsonify({
        "id": contract_id, "numero_contrato": contract_number, "paciente": form["nome_paciente"],
        "telefone": form["telefone"], "telefone_whatsapp": _normalize_phone(form["telefone"]),
        "criado_em": _display_date(_iso(now), True), "valido_ate": _display_date(_iso(expires)),
        "validade_dias": 7, "url": public_url,
    }), 201


@app.post("/api/contracts/<contract_id>/sent")
@login_required
def mark_sent(contract_id):
    _check_csrf()
    row = store.get_by_id(contract_id)
    if not row:
        return jsonify({"erro": "Contrato não encontrado."}), 404
    changed = store.mark_sent(contract_id, _audit_actor(), _iso(_now()))
    current = store.get_by_id(contract_id)
    return jsonify({"ok": True, "alterado": changed, "status": current["status"],
                    "status_label": STATUS_LABELS[current["status"]]})


def _legacy_fernet(secret):
    return Fernet(_key_from_secret(secret))


def _decode_legacy_token(token):
    if len(token) > 25000:
        raise InvalidToken
    candidates = [os.environ.get("LEGACY_SIGNING_SECRET", "gerador-contratos-local"),
                  os.environ.get("SIGNING_SECRET", "")]
    last_error = InvalidToken()
    for secret in dict.fromkeys(value for value in candidates if value):
        cipher = _legacy_fernet(secret)
        try:
            timestamp = datetime.fromtimestamp(cipher.extract_timestamp(token.encode("ascii")), timezone.utc)
            compatibility_until = os.environ.get("LEGACY_COMPATIBILITY_UNTIL", "").strip()
            if compatibility_until and _now() > _parse_iso(compatibility_until):
                raise TimeoutError
            if timestamp > _now() + timedelta(seconds=60):
                raise InvalidToken
            if _now() - timestamp > timedelta(seconds=LINK_TTL_SECONDS):
                raise TimeoutError
            raw = cipher.decrypt(token.encode("ascii"))
            if raw.startswith(b"Z"):
                decompressor = zlib.decompressobj()
                unpacked = decompressor.decompress(raw[1:], 20001)
                if len(unpacked) > 20000 or not decompressor.eof:
                    raise InvalidToken
                compact = json.loads(unpacked.decode("utf-8"))
                data = {CODE_FIELDS[code]: value for code, value in compact.items() if code in CODE_FIELDS}
            else:
                data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict) or any(field not in FORM_FIELDS for field in data):
                raise InvalidToken
            return _safe_form(data)
        except TimeoutError:
            raise
        except (InvalidToken, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            last_error = exc
    raise last_error


def _contract_from_token(token):
    if len(token) > 25000:
        return None, None, "invalid"
    row = None
    if len(token) <= 128 and re.fullmatch(r"[A-Za-z0-9_-]+", token):
        row = store.get_by_token_hash(hashlib.sha256(token.encode()).hexdigest())
    if row:
        if _parse_iso(row["expires_at"]) <= _now() and row["status"] != "ASSINADO":
            store.mark_expired(row["id"], _iso(_now()))
            return row, None, "expired"
        if row["signed_at"]:
            return row, None, "signed"
        try:
            return row, _decrypt_form(row["data_ciphertext"]), "valid"
        except (InvalidToken, ValueError, json.JSONDecodeError):
            app.logger.exception("Falha ao descriptografar contrato %s", row["id"])
            return row, None, "invalid"
    try:
        return None, _decode_legacy_token(token), "legacy"
    except TimeoutError:
        return None, None, "expired"
    except (InvalidToken, ValueError, UnicodeError, json.JSONDecodeError):
        return None, None, "invalid"


@app.route("/assinar/<token>", methods=["GET", "POST"])
def sign_contract(token):
    limited = _rate_limit("sign-contract", 80 if request.method == "GET" else 12, 15 * 60)
    if limited:
        return limited
    row, form, state = _contract_from_token(token)
    if state in ("invalid", "expired", "signed"):
        status_code = 404 if state == "invalid" else 410 if state == "expired" else 200
        return render_template("link_status.html", state=state), status_code
    if request.method == "GET":
        if row:
            store.mark_viewed(row["id"], _iso(_now()))
        return render_template("assinar.html", contrato=form, legacy=state == "legacy")
    _check_csrf()
    assinatura = request.form.get("assinatura_paciente", "")
    aceite = request.form.get("aceite") == "sim"
    if not aceite or not assinatura:
        return jsonify({"erro": "Confirme o aceite e faça sua assinatura antes de continuar."}), 400
    if len(assinatura) > 3_000_000 or not assinatura.startswith("data:image/png;base64,"):
        return jsonify({"erro": "A assinatura recebida é inválida. Limpe o campo e tente novamente."}), 400
    form["assinatura_paciente"] = assinatura
    try:
        response = _download(form, signed=True)
    except ValueError as exc:
        return jsonify({"erro": str(exc)}), 400
    except Exception:
        app.logger.exception("Falha ao gerar contrato assinado")
        return jsonify({"erro": "Não foi possível concluir a assinatura. Tente novamente."}), 500
    if row and not store.mark_signed(row["id"], _iso(_now())):
        current = store.get_by_id(row["id"])
        if current and current["status"] == "EXPIRADO":
            return jsonify({"erro": "Este link expirou e não aceita mais assinaturas."}), 410
        return jsonify({"erro": "Este contrato já foi assinado e não aceita uma nova assinatura."}), 409
    return response


@app.get("/privacidade")
def privacy():
    return render_template("privacidade.html")


@app.get("/health")
def health():
    try:
        backend = store.health()
        return jsonify({"status": "ok", "storage": backend,
                        "admin_configured": bool(os.environ.get("ADMIN_PASSWORD_HASH"))})
    except Exception:
        app.logger.exception("Falha no health check")
        return jsonify({"status": "error"}), 503


@app.errorhandler(400)
def bad_request(error):
    if request.path.startswith("/api/"):
        return jsonify({"erro": getattr(error, "description", "Requisição inválida.")}), 400
    return render_template("link_status.html", state="request_error"), 400


@app.errorhandler(413)
def payload_too_large(_):
    return jsonify({"erro": "Os dados enviados ultrapassam o limite permitido."}), 413


@app.errorhandler(404)
def not_found(_):
    return render_template("link_status.html", state="not_found"), 404


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
