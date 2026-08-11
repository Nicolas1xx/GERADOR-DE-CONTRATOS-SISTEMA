from datetime import date
from io import BytesIO

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm


PLACEHOLDERS = {
    "{{nomeContratante}}": "nome_contratante",
    "{{cpfContratante}}": "cpf_contratante",
    "{{cidadeContratante}}": "cidade_contratante",
    "{{enderecoContratante}}": "endereco_contratante",
    "{{bairroContratante}}": "bairro_contratante",
    "{{cepContratante}}": "cep_contratante",
    "{{nomeContratada}}": "nome_contratada",
    "{{cpfCnpjContratada}}": "cpf_cnpj_contratada",
    "{{nomePaciente}}": "nome_paciente",
    "{{procedimentos}}": "procedimentos",
    "{{valor}}": "valor",
    "{{cidadeClinica}}": "cidade_clinica",
    "{{date}}": "data_contrato",
}


def _clean(value):
    return " ".join((value or "").strip().split())


def _replace_in_paragraph(paragraph, replacements):
    for run in paragraph.runs:
        for token, value in replacements.items():
            if token in run.text:
                run.text = run.text.replace(token, value)


def _all_paragraphs(document):
    yield from document.paragraphs
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from cell.paragraphs
    for section in document.sections:
        yield from section.header.paragraphs
        yield from section.footer.paragraphs


def _insert_logo(document, image_source):
    image_source.seek(0)
    for index, section in enumerate(document.sections):
        if index and section.header.is_linked_to_previous:
            continue
        paragraph = section.header.paragraphs[0] if section.header.paragraphs else section.header.add_paragraph()
        paragraph.clear()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
        paragraph.add_run().add_picture(image_source, width=Cm(2.7))
        image_source.seek(0)


def generate_contract_docx(form, template_source, image_source=None):
    if hasattr(template_source, "seek"):
        template_source.seek(0)
    try:
        document = Document(template_source)
    except Exception as exc:
        raise ValueError("Não foi possível abrir o contrato-base. Use um arquivo Word .docx válido.") from exc

    replacements = {token: _clean(form.get(field)) for token, field in PLACEHOLDERS.items()}
    for paragraph in _all_paragraphs(document):
        _replace_in_paragraph(paragraph, replacements)

    remaining = sorted({token for paragraph in _all_paragraphs(document) for token in PLACEHOLDERS if token in paragraph.text})
    if remaining:
        raise ValueError("Alguns campos do modelo não puderam ser preenchidos: " + ", ".join(remaining))

    if image_source:
        try:
            _insert_logo(document, image_source)
        except Exception as exc:
            raise ValueError("Não foi possível inserir o logotipo. Use uma imagem PNG ou JPG válida.") from exc

    output = BytesIO()
    document.save(output)
    output.seek(0)
    return output


def default_contract_form():
    hoje = date.today()
    meses = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]
    return {
        "nome_contratante": "",
        "cpf_contratante": "",
        "cidade_contratante": "",
        "endereco_contratante": "",
        "bairro_contratante": "",
        "cep_contratante": "",
        "nome_contratada": "Consultório Odontológico Dr. Ângelo G. Martinez",
        "cpf_cnpj_contratada": "",
        "nome_paciente": "",
        "procedimentos": "",
        "valor": "R$ ",
        "cidade_clinica": "",
        "data_contrato": f"{hoje.day:02d} de {meses[hoje.month - 1]} de {hoje.year}",
    }
