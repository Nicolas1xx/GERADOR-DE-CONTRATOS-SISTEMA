const form = document.getElementById('contractForm');
const generateButton = document.getElementById('generateButton');
const modal = document.getElementById('successModal');
const formStatus = document.getElementById('formStatus');
let currentContract = null;

const digits = value => (value || '').replace(/\D/g, '');
const masks = {
  cpf(value) { const d = digits(value).slice(0, 11); return d.replace(/(\d{3})(\d)/, '$1.$2').replace(/(\d{3})(\d)/, '$1.$2').replace(/(\d{3})(\d{1,2})$/, '$1-$2'); },
  phone(value) {
    let d = String(value || '').replace(/\D/g, '');
    if (d.startsWith('55') && (d.length === 12 || d.length === 13)) d = d.slice(2);
    d = d.slice(0, 11);
    if (d.length <= 2) return d ? `(${d}` : '';
    const ddd = d.slice(0, 2), subscriber = d.slice(2);
    if (subscriber.length <= 4) return `(${ddd}) ${subscriber}`;
    const split = subscriber.length > 8 ? 5 : 4;
    return `(${ddd}) ${subscriber.slice(0, split)}-${subscriber.slice(split)}`;
  },
  cep(value) { return digits(value).slice(0, 8).replace(/(\d{5})(\d)/, '$1-$2'); },
  date(value) { return digits(value).slice(0, 8).replace(/(\d{2})(\d)/, '$1/$2').replace(/(\d{2})(\d)/, '$1/$2'); },
  document(value) { const d = digits(value).slice(0, 14); return d.length <= 11 ? masks.cpf(d) : d.replace(/(\d{2})(\d)/, '$1.$2').replace(/(\d{3})(\d)/, '$1.$2').replace(/(\d{3})(\d)/, '$1/$2').replace(/(\d{4})(\d{1,2})$/, '$1-$2'); },
  money(value) { const d = digits(value).slice(0, 15); if (!d) return ''; const amount = (Number(d) / 100).toFixed(2).replace('.', ','); const [whole, cents] = amount.split(','); return `${Number(whole).toLocaleString('pt-BR')},${cents}`; }
};

document.querySelectorAll('[data-mask]').forEach(input => {
  const apply = () => { const formatter = masks[input.dataset.mask]; if (formatter) input.value = formatter(input.value); };
  input.addEventListener('input', () => window.setTimeout(apply, 0));
  apply();
});

function validCpf(value) {
  const cpf = digits(value);
  if (cpf.length !== 11 || /^(\d)\1+$/.test(cpf)) return false;
  for (let size = 9; size <= 10; size++) {
    let total = 0;
    for (let i = 0; i < size; i++) total += Number(cpf[i]) * (size + 1 - i);
    if (((total * 10) % 11) % 10 !== Number(cpf[size])) return false;
  }
  return true;
}

function normalizeBrazilPhone(value) {
  let number = digits(value);
  if (number.startsWith('55') && (number.length === 12 || number.length === 13)) number = number.slice(2);
  if (![10, 11].includes(number.length) || number[0] === '0' || /^(\d)\1+$/.test(number)) return null;
  const subscriber = number.slice(2);
  if (subscriber.length === 9 && subscriber[0] !== '9') return null;
  if (subscriber.length === 8 && !'2345'.includes(subscriber[0])) return null;
  return `55${number}`;
}

function clearErrors() {
  document.querySelectorAll('.field-error').forEach(error => { if (error.id !== 'phoneError') error.textContent = ''; });
  form.querySelectorAll('[aria-invalid="true"]').forEach(input => input.removeAttribute('aria-invalid'));
}

function showFieldErrors(errors) {
  Object.entries(errors || {}).forEach(([name, message]) => {
    const input = form.elements[name];
    const target = document.querySelector(`[data-error-for="${name}"]`);
    if (input) input.setAttribute('aria-invalid', 'true');
    if (target) target.textContent = message;
  });
  const first = form.querySelector('[aria-invalid="true"]');
  if (first) first.focus({ preventScroll: true });
  first?.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function clientValidation() {
  clearErrors();
  const errors = {};
  for (const input of form.querySelectorAll('input[required], textarea[required]')) {
    if (!input.value.trim()) errors[input.name] = 'Preencha este campo obrigatório.';
  }
  if (form.cpf_contratante.value && !validCpf(form.cpf_contratante.value)) errors.cpf_contratante = 'Informe um CPF válido.';
  if (form.telefone.value && !normalizeBrazilPhone(form.telefone.value)) errors.telefone = 'Informe um telefone válido com DDD.';
  if (form.email.value && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(form.email.value)) errors.email = 'Informe um e-mail válido.';
  if (Object.keys(errors).length) { showFieldErrors(errors); formStatus.textContent = 'Revise os campos destacados antes de continuar.'; return false; }
  return true;
}

function defaultMessage(data) {
  return `Olá, ${data.paciente}! Tudo bem?\n\nSeu contrato foi preparado e está disponível para conferência e assinatura digital.\n\nPara visualizar e assinar o documento, acesse o link abaixo:\n\n${data.url}\n\nO link é individual e destinado exclusivamente ao titular do contrato.\n\nPor segurança, ele ficará disponível por 7 dias.\n\nEm caso de dúvidas, entre em contato conosco.\n\nObrigado!`;
}

function openModal(data) {
  currentContract = data;
  document.getElementById('summaryPatient').textContent = data.paciente;
  document.getElementById('summaryNumber').textContent = data.numero_contrato;
  document.getElementById('summaryCreated').textContent = data.criado_em;
  document.getElementById('summaryExpires').textContent = data.valido_ate;
  document.getElementById('signatureLink').value = data.url;
  const formPhone = form.elements.namedItem('telefone')?.value || data.telefone || '';
  document.getElementById('recipientPhone').value = masks.phone(formPhone);
  document.getElementById('whatsappMessage').value = defaultMessage(data);
  document.getElementById('phoneError').textContent = '';
  document.getElementById('sendStatus').textContent = '';
  modal.hidden = false;
  document.body.classList.add('modal-open');
  document.getElementById('copyLink').focus();
}

function closeModal() {
  modal.hidden = true;
  document.body.classList.remove('modal-open');
}

generateButton.addEventListener('click', async () => {
  if (!clientValidation()) return;
  generateButton.disabled = true;
  generateButton.textContent = 'Gerando contrato...';
  formStatus.textContent = 'Criando o link individual e seguro.';
  try {
    const response = await fetch('/api/contracts', { method: 'POST', body: new FormData(form) });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { showFieldErrors(data.campos); throw new Error(data.erro || 'Não foi possível criar o contrato.'); }
    formStatus.textContent = '';
    openModal(data);
  } catch (error) {
    formStatus.textContent = error.message || 'Não foi possível criar o contrato. Tente novamente.';
  } finally {
    generateButton.disabled = false;
    generateButton.textContent = 'Gerar contrato para assinatura';
  }
});

document.getElementById('copyLink').addEventListener('click', async event => {
  const link = document.getElementById('signatureLink').value;
  try { await navigator.clipboard.writeText(link); event.currentTarget.textContent = 'Link copiado'; }
  catch { document.getElementById('signatureLink').select(); document.execCommand('copy'); event.currentTarget.textContent = 'Link copiado'; }
  setTimeout(() => { event.currentTarget.textContent = 'Copiar link'; }, 1800);
});

document.getElementById('sendWhatsapp').addEventListener('click', async () => {
  const phoneInput = document.getElementById('recipientPhone');
  const normalized = normalizeBrazilPhone(phoneInput.value);
  const phoneError = document.getElementById('phoneError');
  const message = document.getElementById('whatsappMessage').value.trim();
  phoneError.textContent = '';
  if (!normalized) { phoneError.textContent = 'Informe um telefone brasileiro válido com DDD.'; phoneInput.focus(); return; }
  if (!message || !message.includes(currentContract.url)) { document.getElementById('sendStatus').textContent = 'Mantenha o link de assinatura na mensagem antes de enviar.'; return; }
  const popup = window.open(`https://wa.me/${normalized}?text=${encodeURIComponent(message)}`, '_blank', 'noopener,noreferrer');
  if (!popup) { document.getElementById('sendStatus').textContent = 'O navegador bloqueou a nova janela. Permita pop-ups e tente novamente.'; return; }
  try { await fetch(`/api/contracts/${currentContract.id}/sent`, { method: 'POST', headers: { 'X-CSRF-Token': document.getElementById('csrfToken').value } }); } catch { /* O WhatsApp já foi aberto; o registro pode ser tentado novamente. */ }
});

document.getElementById('closeModal').addEventListener('click', closeModal);
document.getElementById('newContract').addEventListener('click', () => window.location.reload());
modal.addEventListener('click', event => { if (event.target === modal) closeModal(); });
document.addEventListener('keydown', event => { if (event.key === 'Escape' && !modal.hidden) closeModal(); });
