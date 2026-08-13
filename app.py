import os
import re

from flask import Flask, flash, render_template, request, send_file
from contract_generator import default_contract_form, generate_contract_docx


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE = os.path.join(BASE_DIR, "modelo_contrato_odontologico.docx")
DEFAULT_LOGO = os.path.join(BASE_DIR, "logo_consultorio_angelo.jpg")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "gerador-contratos-local")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024


@app.get("/")
def index():
    return render_template("index.html", valores=default_contract_form())


@app.get("/logo")
def logo():
    return send_file(DEFAULT_LOGO, mimetype="image/jpeg")


@app.post("/gerar")
def gerar():
    obrigatorios = {
        "nome_contratante": "Nome do contratante",
        "cpf_contratante": "CPF do contratante",
        "rg_contratante": "RG do contratante",
        "cidade_contratante": "Cidade do contratante",
        "endereco_contratante": "Endereço do contratante",
        "numero": "Número do endereço",
        "bairro_contratante": "Bairro",
        "cep_contratante": "CEP",
        "telefone": "Telefone",
        "email": "E-mail",
        "nome_contratada": "Nome da clínica/contratada",
        "cpf_cnpj_contratada": "CPF/CNPJ da contratada",
        "cro": "CRO da contratada",
        "endereco_clinica": "Endereço profissional da clínica",
        "nome_paciente": "Nome do paciente",
        "procedimentos": "Procedimentos",
        "valor_total": "Valor total do tratamento",
        "forma_pagamento": "Forma de pagamento",
        "parcelas": "Número de parcelas",
        "vencimentos": "Vencimentos",
        "valor_avaliacao": "Valor da avaliação",
        "limite_atraso": "Limite de atraso",
        "cidade_clinica": "Cidade da clínica",
        "data_contrato": "Data do contrato",
    }
    faltando = [rotulo for campo, rotulo in obrigatorios.items() if not request.form.get(campo, "").strip()]
    if faltando:
        flash("Preencha os campos obrigatórios: " + ", ".join(faltando) + ".", "erro")
        return render_template("index.html", valores=request.form), 400

    cpf = re.sub(r"\D", "", request.form.get("cpf_contratante", ""))
    documento_clinica = re.sub(r"\D", "", request.form.get("cpf_cnpj_contratada", ""))
    cep = re.sub(r"\D", "", request.form.get("cep_contratante", ""))
    telefone = re.sub(r"\D", "", request.form.get("telefone", ""))
    erros = []
    if len(cpf) != 11:
        erros.append("o CPF do contratante deve ter 11 números")
    if len(documento_clinica) not in (11, 14):
        erros.append("o CPF/CNPJ da contratada deve ter 11 ou 14 números")
    if len(cep) != 8:
        erros.append("o CEP deve ter 8 números")
    if len(telefone) not in (10, 11):
        erros.append("o telefone deve ter DDD e 10 ou 11 números")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", request.form.get("email", "").strip()):
        erros.append("informe um e-mail válido")
    if not re.search(r"\d", request.form.get("valor_total", "")):
        erros.append("informe um valor válido para o tratamento")
    if not re.search(r"\d", request.form.get("valor_avaliacao", "")):
        erros.append("informe um valor válido para a avaliação")
    try:
        if not 1 <= int(request.form.get("limite_atraso", "0")) <= 180:
            raise ValueError
    except ValueError:
        erros.append("o limite de atraso deve estar entre 1 e 180 minutos")
    if len(request.form.get("procedimentos", "").strip()) < 3:
        erros.append("descreva os procedimentos odontológicos")
    if erros:
        flash("Revise os dados: " + "; ".join(erros) + ".", "erro")
        return render_template("index.html", valores=request.form), 400

    template_source = DEFAULT_TEMPLATE

    try:
        arquivo = generate_contract_docx(request.form, template_source)
    except Exception as exc:
        flash(str(exc), "erro")
        return render_template("index.html", valores=request.form), 400

    nome = re.sub(r"[^A-Za-z0-9_-]+", "_", request.form["nome_paciente"].strip()).strip("_") or "paciente"
    return send_file(
        arquivo,
        as_attachment=True,
        download_name=f"contrato_{nome}.docx",
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@app.errorhandler(413)
def arquivo_grande(_):
    flash("Os arquivos enviados ultrapassam o limite de 20 MB.", "erro")
    return render_template("index.html", valores=request.form), 413


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
