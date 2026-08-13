import base64
import hashlib
import json
import os
import re
import zlib
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, flash, jsonify, render_template, request, send_file
from werkzeug.middleware.proxy_fix import ProxyFix

from contract_generator import PLACEHOLDERS, default_contract_form, generate_contract_docx


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE = os.path.join(BASE_DIR, "modelo_contrato_odontologico.docx")
DEFAULT_LOGO = os.path.join(BASE_DIR, "logo_consultorio_angelo.jpg")
LINK_TTL_SECONDS = 7 * 24 * 60 * 60
FORM_FIELDS = sorted(set(PLACEHOLDERS.values()) | {"data_contrato"})
FIELD_CODES = {field: format(index, "x") for index, field in enumerate(FORM_FIELDS)}
CODE_FIELDS = {code: field for field, code in FIELD_CODES.items()}
FIELD_LIMITS = {field: 180 for field in FORM_FIELDS} | {
    "email": 150, "procedimentos": 1500, "complemento": 80, "numero": 7,
    "cpf_contratante": 14, "cpf_cnpj_contratada": 18, "cep_contratante": 9,
    "telefone": 15, "rg_contratante": 12, "cro": 18, "data_contrato": 10,
    "valor_total": 25, "valor_avaliacao": 25, "limite_atraso": 3,
}

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SECRET_KEY", "gerador-contratos-local")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024


def _fernet():
    secret = os.environ.get("SIGNING_SECRET") or os.environ.get("SECRET_KEY") or "gerador-contratos-local"
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


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
    return bool(re.fullmatch(r"(?:R\$\s*)?\d{1,3}(?:\.\d{3})*,\d{2}", (value or "").strip()))


def _validate(form):
    required = {
        "nome_contratante": "Nome do contratante", "cpf_contratante": "CPF do contratante",
        "rg_contratante": "RG do contratante", "cidade_contratante": "Cidade do contratante",
        "endereco_contratante": "Endereço do contratante", "numero": "Número do endereço",
        "bairro_contratante": "Bairro", "cep_contratante": "CEP", "telefone": "Telefone",
        "email": "E-mail", "nome_contratada": "Nome da clínica/contratada",
        "cpf_cnpj_contratada": "CPF/CNPJ da contratada", "cro": "CRO da contratada",
        "endereco_clinica": "Endereço profissional da clínica", "nome_paciente": "Nome do paciente",
        "procedimentos": "Procedimentos", "valor_total": "Valor total do tratamento",
        "forma_pagamento": "Forma de pagamento", "parcelas": "Número de parcelas",
        "vencimentos": "Vencimentos", "valor_avaliacao": "Valor da avaliação",
        "limite_atraso": "Limite de atraso", "cidade_clinica": "Cidade da clínica",
        "data_contrato": "Data do contrato",
    }
    missing = [label for field, label in required.items() if not form.get(field, "").strip()]
    if missing:
        return ["Preencha os campos obrigatórios: " + ", ".join(missing) + "."]

    digits = lambda field: re.sub(r"\D", "", form.get(field, ""))
    errors = []
    for field, limit in FIELD_LIMITS.items():
        if len(form.get(field, "").strip()) > limit:
            errors.append(f"o campo {required.get(field, field)} ultrapassa {limit} caracteres")
    if not _valid_cpf(form.get("cpf_contratante")):
        errors.append("informe um CPF válido")
    clinic_document = digits("cpf_cnpj_contratada")
    if not (_valid_cpf(clinic_document) if len(clinic_document) == 11 else _valid_cnpj(clinic_document)):
        errors.append("informe um CPF/CNPJ válido para a contratada")
    if len(digits("cep_contratante")) != 8:
        errors.append("o CEP deve ter 8 números")
    if len(digits("telefone")) not in (10, 11):
        errors.append("o telefone deve ter DDD e 10 ou 11 números")
    if len(digits("rg_contratante")) < 7:
        errors.append("informe um RG válido")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", form.get("email", "").strip()):
        errors.append("informe um e-mail válido")
    if not _valid_money(form.get("valor_total")):
        errors.append("informe o valor total no formato 0,00")
    if not _valid_money(form.get("valor_avaliacao")):
        errors.append("informe o valor da avaliação no formato 0,00")
    try:
        if not 1 <= int(form.get("limite_atraso", "0")) <= 180:
            raise ValueError
    except ValueError:
        errors.append("o limite de atraso deve estar entre 1 e 180 minutos")
    if len(form.get("procedimentos", "").strip()) < 3:
        errors.append("descreva os procedimentos odontológicos")
    for field, label in (("nome_contratante", "nome do contratante"), ("nome_paciente", "nome do paciente")):
        value = form.get(field, "").strip()
        if not re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ' -]{5,120}", value) or " " not in value:
            errors.append(f"informe o {label} completo")
    if not re.fullmatch(r"\d{1,6}[A-Za-z]?", form.get("numero", "").strip()):
        errors.append("informe um número de endereço válido")
    if not re.fullmatch(r"CRO-[A-Z]{2}\s\d{3,8}", form.get("cro", "").strip().upper()):
        errors.append("informe o CRO no formato CRO-UF 12345")
    for field, label in (("cidade_contratante", "cidade do contratante"), ("cidade_clinica", "cidade da clínica")):
        city = form.get(field, "").strip()
        parts = city.rsplit("/", 1)
        if len(parts) != 2 or len(parts[0].strip()) < 2 or not re.fullmatch(r"[A-Za-z]{2}", parts[1]):
            errors.append(f"informe a {label} no formato Cidade/UF")
    try:
        datetime.strptime(form.get("data_contrato", ""), "%d/%m/%Y")
    except ValueError:
        errors.append("informe uma data válida no formato DD/MM/AAAA")
    return errors


def _safe_form(form):
    cleaned = {field: form.get(field, "").strip()[:FIELD_LIMITS[field]] for field in FORM_FIELDS}
    cleaned["cro"] = cleaned["cro"].upper()
    return cleaned


def _download(form):
    with open(DEFAULT_LOGO, "rb") as logo_file:
        arquivo = generate_contract_docx(form, DEFAULT_TEMPLATE, logo_file)
    nome = re.sub(r"[^A-Za-z0-9_-]+", "_", form["nome_paciente"].strip()).strip("_") or "paciente"
    return send_file(arquivo, as_attachment=True, download_name=f"contrato_{nome}.docx",
                     mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@app.get("/")
def index():
    return render_template("index.html", valores=default_contract_form())


@app.get("/logo")
def logo():
    return send_file(DEFAULT_LOGO, mimetype="image/jpeg")


@app.post("/gerar")
def gerar():
    errors = _validate(request.form)
    if errors:
        flash("Revise os dados: " + "; ".join(errors), "erro")
        return render_template("index.html", valores=request.form), 400
    try:
        return _download(_safe_form(request.form))
    except Exception as exc:
        flash(str(exc), "erro")
        return render_template("index.html", valores=request.form), 400


@app.post("/criar-link")
def criar_link():
    errors = _validate(request.form)
    if errors:
        return jsonify({"erro": "Revise os dados: " + "; ".join(errors)}), 400
    compact = {FIELD_CODES[field]: value for field, value in _safe_form(request.form).items()}
    payload = json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    token = _fernet().encrypt(b"Z" + zlib.compress(payload, level=9)).decode("ascii")
    url = request.url_root.rstrip("/") + "/assinar/" + token
    return jsonify({"url": url, "validade_dias": 7})


def _decode_token(token):
    if len(token) > 25000:
        raise InvalidToken
    raw = _fernet().decrypt(token.encode("ascii"), ttl=LINK_TTL_SECONDS)
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


@app.route("/assinar/<token>", methods=["GET", "POST"])
def assinar(token):
    try:
        form = _decode_token(token)
    except (InvalidToken, ValueError, UnicodeError, json.JSONDecodeError):
        return render_template("assinar.html", expirado=True), 410

    if request.method == "GET":
        return render_template("assinar.html", contrato=form, token=token, expirado=False)

    assinatura = request.form.get("assinatura_paciente", "")
    aceite = request.form.get("aceite") == "sim"
    if not aceite or not assinatura:
        return jsonify({"erro": "Confirme o aceite e faça sua assinatura antes de continuar."}), 400
    form["assinatura_paciente"] = assinatura
    try:
        return _download(form)
    except Exception as exc:
        return jsonify({"erro": str(exc)}), 400


@app.errorhandler(413)
def arquivo_grande(_):
    return jsonify({"erro": "Os dados enviados ultrapassam o limite permitido."}), 413


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
