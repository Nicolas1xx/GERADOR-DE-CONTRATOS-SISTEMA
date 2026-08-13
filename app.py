import base64
import hashlib
import json
import os
import re

from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, flash, jsonify, render_template, request, send_file
from werkzeug.middleware.proxy_fix import ProxyFix

from contract_generator import PLACEHOLDERS, default_contract_form, generate_contract_docx


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE = os.path.join(BASE_DIR, "modelo_contrato_odontologico.docx")
DEFAULT_LOGO = os.path.join(BASE_DIR, "logo_consultorio_angelo.jpg")
LINK_TTL_SECONDS = 7 * 24 * 60 * 60
FORM_FIELDS = sorted(set(PLACEHOLDERS.values()) | {"data_contrato"})

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SECRET_KEY", "gerador-contratos-local")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024


def _fernet():
    secret = os.environ.get("SIGNING_SECRET") or os.environ.get("SECRET_KEY") or "gerador-contratos-local"
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


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
    if len(digits("cpf_contratante")) != 11:
        errors.append("o CPF do contratante deve ter 11 números")
    if len(digits("cpf_cnpj_contratada")) not in (11, 14):
        errors.append("o CPF/CNPJ da contratada deve ter 11 ou 14 números")
    if len(digits("cep_contratante")) != 8:
        errors.append("o CEP deve ter 8 números")
    if len(digits("telefone")) not in (10, 11):
        errors.append("o telefone deve ter DDD e 10 ou 11 números")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", form.get("email", "").strip()):
        errors.append("informe um e-mail válido")
    if not re.search(r"\d", form.get("valor_total", "")):
        errors.append("informe um valor válido para o tratamento")
    if not re.search(r"\d", form.get("valor_avaliacao", "")):
        errors.append("informe um valor válido para a avaliação")
    try:
        if not 1 <= int(form.get("limite_atraso", "0")) <= 180:
            raise ValueError
    except ValueError:
        errors.append("o limite de atraso deve estar entre 1 e 180 minutos")
    if len(form.get("procedimentos", "").strip()) < 3:
        errors.append("descreva os procedimentos odontológicos")
    return errors


def _safe_form(form):
    return {field: form.get(field, "").strip() for field in FORM_FIELDS}


def _download(form):
    arquivo = generate_contract_docx(form, DEFAULT_TEMPLATE)
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
    payload = json.dumps(_safe_form(request.form), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    token = _fernet().encrypt(payload).decode("ascii")
    url = request.url_root.rstrip("/") + "/assinar/" + token
    return jsonify({"url": url, "validade_dias": 7})


def _decode_token(token):
    if len(token) > 25000:
        raise InvalidToken
    raw = _fernet().decrypt(token.encode("ascii"), ttl=LINK_TTL_SECONDS)
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
