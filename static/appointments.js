const appointmentDigits = value => String(value || '').replace(/\D/g, '');
const appointmentMasks = {
  cpf(value) { const d = appointmentDigits(value).slice(0, 11); return d.replace(/(\d{3})(\d)/, '$1.$2').replace(/(\d{3})(\d)/, '$1.$2').replace(/(\d{3})(\d{1,2})$/, '$1-$2'); },
  phone(value) { let d = appointmentDigits(value); if (d.startsWith('55') && (d.length === 12 || d.length === 13)) d = d.slice(2); d = d.slice(0, 11); if (d.length <= 2) return d ? `(${d}` : ''; const subscriber = d.slice(2), split = subscriber.length > 8 ? 5 : 4; return `(${d.slice(0, 2)}) ${subscriber.slice(0, split)}${subscriber.length > split ? `-${subscriber.slice(split)}` : ''}`; }
};

document.querySelectorAll('[data-appointment-mask]').forEach(input => {
  const apply = () => { const mask = appointmentMasks[input.dataset.appointmentMask]; if (mask) input.value = mask(input.value); };
  input.addEventListener('input', () => window.setTimeout(apply, 0));
  apply();
});

function openAppointmentModal(id) { const modal = document.getElementById(id); if (!modal) return; modal.hidden = false; document.body.classList.add('modal-open'); modal.querySelector('input:not([type=hidden]),select,textarea')?.focus(); }
function closeAppointmentModal(modal) { if (!modal) return; modal.hidden = true; document.body.classList.remove('modal-open'); }
document.querySelectorAll('[data-open-modal]').forEach(button => button.addEventListener('click', () => openAppointmentModal(button.dataset.openModal)));
document.querySelectorAll('[data-close-modal]').forEach(button => button.addEventListener('click', () => closeAppointmentModal(button.closest('.modal-backdrop'))));
document.querySelectorAll('.appointment-modal-backdrop').forEach(modal => modal.addEventListener('click', event => { if (event.target === modal) closeAppointmentModal(modal); }));
document.addEventListener('keydown', event => { if (event.key === 'Escape') document.querySelectorAll('.appointment-modal-backdrop:not([hidden])').forEach(closeAppointmentModal); });

const periodSelect = document.getElementById('appointment-period');
function toggleCustomPeriod() { document.querySelectorAll('[data-custom-period]').forEach(field => { field.hidden = periodSelect?.value !== 'custom'; }); }
periodSelect?.addEventListener('change', toggleCustomPeriod);
toggleCustomPeriod();

document.querySelectorAll('form[data-confirm]').forEach(form => form.addEventListener('submit', event => { if (!window.confirm(form.dataset.confirm)) event.preventDefault(); }));

function normalizeAppointmentPhone(value) {
  let number = appointmentDigits(value);
  if (number.startsWith('55') && (number.length === 12 || number.length === 13)) number = number.slice(2);
  if (![10, 11].includes(number.length) || number[0] === '0' || /^(\d)\1+$/.test(number)) return null;
  const subscriber = number.slice(2);
  if (subscriber.length === 9 && subscriber[0] !== '9') return null;
  if (subscriber.length === 8 && !'2345'.includes(subscriber[0])) return null;
  return `55${number}`;
}

document.querySelector('[data-appointment-whatsapp]')?.addEventListener('click', async event => {
  const button = event.currentTarget;
  const phoneInput = document.getElementById('appointment-whatsapp-phone');
  const messageInput = document.getElementById('appointment-whatsapp-message');
  const status = document.getElementById('appointmentWhatsappStatus');
  const phone = normalizeAppointmentPhone(phoneInput.value);
  const message = messageInput.value.trim();
  status.textContent = '';
  if (!phone) { status.textContent = 'Informe um telefone brasileiro válido com DDD.'; phoneInput.focus(); return; }
  if (!message) { status.textContent = 'Escreva a mensagem de confirmação.'; messageInput.focus(); return; }
  const link = document.createElement('a');
  link.href = `https://wa.me/${phone}?text=${encodeURIComponent(message)}`;
  link.target = '_blank'; link.rel = 'noopener noreferrer'; document.body.appendChild(link); link.click(); link.remove();
  try {
    const response = await fetch(`/api/appointments/${button.dataset.appointmentId}/confirmation-sent`, { method: 'POST', headers: { 'X-CSRF-Token': button.dataset.csrf } });
    if (!response.ok) throw new Error('history-update-failed');
  } catch { status.textContent = 'O WhatsApp foi aberto, mas o histórico de envio não pôde ser atualizado.'; }
});
