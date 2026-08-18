import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from flask import (Flask, abort, flash, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

from contract_generator import PLACEHOLDERS, default_contract_form, generate_contract_docx
from storage import ContractStore


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
    "AGUARDANDO": "Aguardando assinatura", "VISUALIZADO": "Visualizado",
    "ASSINADO": "Assinado", "EXPIRADO": "Expirado",
}
EVENT_LABELS = {
    "CRIADO": "Criado", "ENVIADO": "Envio pelo WhatsApp iniciado",
    "VISUALIZADO": "Visualizado pelo paciente", "ASSINADO": "Assinado",
    "EXPIRADO": "Expirado",
}


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


@app.context_processor
def template_helpers():
    return {"csrf_token": _csrf_token, "status_labels": STATUS_LABELS, "event_labels": EVENT_LABELS}


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
    now = _now()
    contracts = []
    for row in store.list_recent(40):
        if row["status"] != "ASSINADO" and _parse_iso(row["expires_at"]) <= now:
            store.mark_expired(row["id"], _iso(now))
            row["status"] = "EXPIRADO"
        try:
            patient = _decrypt_form(row["data_ciphertext"]).get("nome_paciente", "Paciente")
        except (InvalidToken, ValueError, json.JSONDecodeError):
            patient = "Dados indisponíveis"
        contracts.append({
            "id": row["id"], "number": row["contract_number"], "patient": patient,
            "status": row["status"], "created_at": _display_date(row["created_at"], True),
            "expires_at": _display_date(row["expires_at"]), "created_by": row["created_by"],
            "events": [{**event, "display_date": _display_date(event["created_at"], True)}
                       for event in store.events_for(row["id"])],
        })
    return render_template("index.html", valores=default_contract_form(), contracts=contracts)


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
        return render_template("index.html", valores=request.form, contracts=[], field_errors=errors), 400
    try:
        return _download(_safe_form(request.form))
    except Exception:
        app.logger.exception("Falha ao gerar Word sem assinatura")
        flash("Não foi possível gerar o arquivo Word. Tente novamente.", "erro")
        return render_template("index.html", valores=request.form, contracts=[]), 500


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
        "data_ciphertext": _encrypt_form(form), "created_by": session["admin_user"],
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
    if not store.get_by_id(contract_id):
        return jsonify({"erro": "Contrato não encontrado."}), 404
    store.add_event(contract_id, "ENVIADO", session["admin_user"], _iso(_now()))
    return jsonify({"ok": True})


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
