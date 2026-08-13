# Gerador de Contratos Odontológicos — Dr. Ângelo G. Martinez

1. A clínica preenche os dados do contratante, paciente, tratamento e financeiro.
2. O sistema cria um link protegido, válido por 7 dias.
3. A clínica envia o link ao paciente pelo WhatsApp.
4. O paciente confere os dados, aceita e assina com o dedo ou mouse.
5. O contrato Word assinado é baixado ou compartilhado pelo próprio paciente.

O modelo jurídico reformulado já está configurado. Para produção, defina a variável de ambiente `SIGNING_SECRET` na Vercel com um valor longo e aleatório; ela protege os dados inseridos nos links de assinatura.
