from io import BytesIO
import os
import re

from flask import Flask, flash, render_template, request, send_file
from werkzeug.utils import secure_filename

from contract_generator import default_contract_form, generate_contract_docx


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE = os.path.join(BASE_DIR, "modelo_contrato_prestacao_servicos.docx")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "gerador-contratos-local")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024


@app.get("/")
def index():
    return render_template("index.html", valores=default_contract_form())


@app.post("/gerar")
def gerar():
    obrigatorios = {
        "nome_contratado": "Nome completo",
        "especialidade": "Especialidade",
        "rg": "RG",
        "cpf": "CPF",
        "registro_profissional": "Registro profissional",
        "endereco": "Endereço",
        "municipio": "Município",
        "uf": "UF",
        "numero_contrato": "Número do contrato",
        "licitacao": "Licitação",
        "descricao_servicos": "Descrição dos serviços",
        "carga_horaria": "Carga horária",
        "honorarios": "Honorários",
        "prazo_pagamento": "Prazo de pagamento",
        "multa_rescisao": "Multa de rescisão",
        "data_assinatura": "Local e data da assinatura",
    }
    faltando = [rotulo for campo, rotulo in obrigatorios.items() if not request.form.get(campo, "").strip()]
    if faltando:
        flash("Preencha os campos obrigatórios: " + ", ".join(faltando) + ".", "erro")
        return render_template("index.html", valores=request.form), 400

    modelo = request.files.get("contrato_base")
    imagem = request.files.get("imagem_cabecalho")
    template_source = DEFAULT_TEMPLATE

    if modelo and modelo.filename:
        if not modelo.filename.lower().endswith(".docx"):
            flash("O contrato-base precisa ser um arquivo Word .docx.", "erro")
            return render_template("index.html", valores=request.form), 400
        template_source = BytesIO(modelo.read())

    image_source = None
    if imagem and imagem.filename:
        extensao = os.path.splitext(imagem.filename)[1].lower()
        if extensao not in {".png", ".jpg", ".jpeg"}:
            flash("A imagem precisa estar em PNG, JPG ou JPEG.", "erro")
            return render_template("index.html", valores=request.form), 400
        image_source = BytesIO(imagem.read())

    try:
        arquivo = generate_contract_docx(request.form, template_source, image_source)
    except Exception as exc:
        flash(str(exc), "erro")
        return render_template("index.html", valores=request.form), 400

    nome = re.sub(r"[^A-Za-z0-9_-]+", "_", request.form["nome_contratado"].strip()).strip("_") or "contratado"
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
