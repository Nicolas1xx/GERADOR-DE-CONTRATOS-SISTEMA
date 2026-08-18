# Gerador de Contratos Odontológicos

Sistema do Consultório Odontológico Dr. Ângelo G. Martinez para criação, envio, assinatura e acompanhamento de contratos, além da administração de agendamentos.

## Fluxo

1. Um funcionário autenticado preenche e valida os dados do contrato.
2. O backend salva os dados criptografados e cria um token aleatório; apenas o hash do token é armazenado.
3. A clínica copia o link ou abre o WhatsApp com telefone e mensagem editável.
4. O paciente acessa somente o contrato associado ao token, sem login.
5. O sistema registra criação, início do envio, primeira visualização, assinatura e expiração.
6. Após a assinatura, o mesmo link não permite uma segunda assinatura.

A opção existente de baixar o Word sem assinatura foi preservada.

## Agenda

A área administrativa possui visualizações por dia, semana, mês e lista, pesquisa protegida por tokens HMAC, filtros, indicadores do dia, edição, cancelamento sem exclusão, confirmação rápida, histórico e mensagem editável para WhatsApp. Os dados pessoais do agendamento ficam criptografados.

Conflitos de horário do mesmo profissional são impedidos por uma restrição exclusiva parcial no banco. Agendamentos cancelados permanecem no histórico e liberam o horário para uma nova marcação.

## Desenvolvimento local

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt pytest
$env:ADMIN_USERNAME='admin'
$env:ADMIN_PASSWORD_HASH='<hash gerado com werkzeug.security.generate_password_hash>'
$env:SECRET_KEY='<segredo aleatório>'
$env:DATA_ENCRYPTION_SECRET='<segredo aleatório diferente>'
$env:RATE_LIMIT_SECRET='<segredo aleatório diferente>'
.\.venv\Scripts\python.exe app.py
```

Sem `DATABASE_URL`, o desenvolvimento usa `instance/contracts.db` (SQLite). Em produção serverless, configure PostgreSQL.

## Variáveis de ambiente de produção

- `ADMIN_USERNAME`: usuário administrativo.
- `ADMIN_DISPLAY_NAME` (opcional): nome exibido no cabeçalho e nas trilhas de auditoria; o padrão é `Dr. Ângelo G. Martinez`.
- `ADMIN_PASSWORD_HASH`: hash Werkzeug da senha; nunca armazene a senha em texto puro.
- `SECRET_KEY`: assinatura segura da sessão Flask.
- `DATA_ENCRYPTION_SECRET`: criptografia dos dados pessoais armazenados.
- `RATE_LIMIT_SECRET`: anonimização do identificador usado no rate limiting.
- `DATABASE_URL`: conexão PostgreSQL com TLS.
- `PUBLIC_BASE_URL`: `https://gerador-de-contratos-sistema.vercel.app`.
- `LEGACY_COMPATIBILITY_UNTIL`: instante ISO UTC após o qual links da versão antiga deixam de ser aceitos.

Não altere `DATA_ENCRYPTION_SECRET` depois que contratos novos forem criados, pois ela é necessária para descriptografar os registros. Para rotacioná-la, faça uma migração controlada.

## Banco de dados

O schema é criado de forma idempotente com `CREATE TABLE IF NOT EXISTS`. A implantação não apaga nem substitui tabelas ou registros. As tabelas são:

- `contracts`: metadados, hash do token e conteúdo criptografado.
- `contract_events`: trilha de auditoria sem senhas ou segredos.
- `appointment_types`: tipos extensíveis de atendimento.
- `appointments`: metadados do horário e conteúdo pessoal criptografado; o vínculo opcional com contratos fica preparado para uso futuro.
- `appointment_events`: histórico persistente de criação, alterações e status.
- `appointment_search_terms`: índices cegos HMAC para pesquisa sem expor nome, CPF ou telefone.
- `rate_limits`: contadores temporais dos endpoints sensíveis.

Links criados na versão anterior não dependiam de banco. Eles continuam válidos apenas durante a janela original de 7 dias e a janela de compatibilidade configurada.

## Testes e auditoria

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\bandit.exe -q -r app.py storage.py contract_generator.py
.\.venv\Scripts\pip-audit.exe -r requirements.txt
node --check static/admin.js
node --check static/appointments.js
node --check static/sign.js
```
