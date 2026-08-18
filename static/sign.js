const canvas = document.getElementById('signatureCanvas');
const context = canvas.getContext('2d');
const form = document.getElementById('signForm');
const status = document.getElementById('signStatus');
const button = document.getElementById('signButton');
let drawing = false;
let signed = false;
let contractBlob = null;
let contractUrl = null;

function resizeCanvas() {
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  const oldImage = signed ? canvas.toDataURL() : null;
  canvas.width = rect.width * ratio;
  canvas.height = rect.height * ratio;
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.lineWidth = 2.4;
  context.lineCap = 'round';
  context.strokeStyle = '#111';
  if (oldImage) { const image = new Image(); image.onload = () => context.drawImage(image, 0, 0, rect.width, rect.height); image.src = oldImage; }
}
function point(event) { const rect = canvas.getBoundingClientRect(); const source = event.touches?.[0] || event; return { x: source.clientX - rect.left, y: source.clientY - rect.top }; }
function start(event) { event.preventDefault(); drawing = true; signed = true; const p = point(event); context.beginPath(); context.moveTo(p.x, p.y); }
function move(event) { if (!drawing) return; event.preventDefault(); const p = point(event); context.lineTo(p.x, p.y); context.stroke(); }
function stop() { drawing = false; }
['mousedown', 'touchstart'].forEach(name => canvas.addEventListener(name, start, { passive: false }));
['mousemove', 'touchmove'].forEach(name => canvas.addEventListener(name, move, { passive: false }));
['mouseup', 'mouseleave', 'touchend'].forEach(name => canvas.addEventListener(name, stop));
document.getElementById('clearSignature').addEventListener('click', () => { context.clearRect(0, 0, canvas.width, canvas.height); signed = false; });
window.addEventListener('resize', resizeCanvas);
resizeCanvas();

function croppedSignature() {
  const image = context.getImageData(0, 0, canvas.width, canvas.height);
  const data = image.data;
  let left = canvas.width, top = canvas.height, right = 0, bottom = 0;
  for (let y = 0; y < canvas.height; y++) for (let x = 0; x < canvas.width; x++) {
    const index = (y * canvas.width + x) * 4;
    if (data[index] < 220 || data[index + 1] < 220 || data[index + 2] < 220) { left = Math.min(left, x); top = Math.min(top, y); right = Math.max(right, x); bottom = Math.max(bottom, y); }
  }
  const pad = 24, width = Math.max(1, right - left + 1), height = Math.max(1, bottom - top + 1);
  const output = document.createElement('canvas'); output.width = width + pad * 2; output.height = height + pad * 2;
  const out = output.getContext('2d'); out.fillStyle = '#fff'; out.fillRect(0, 0, output.width, output.height); out.drawImage(canvas, left, top, width, height, pad, pad, width, height);
  return output.toDataURL('image/png');
}
function downloadContract() { if (!contractBlob) return; const anchor = document.createElement('a'); anchor.href = contractUrl; anchor.download = 'contrato_assinado.docx'; document.body.appendChild(anchor); anchor.click(); anchor.remove(); }

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (!signed) { status.textContent = 'Faça sua assinatura no campo branco antes de continuar.'; return; }
  if (!form.reportValidity()) { status.textContent = 'Marque a confirmação de leitura para assinar.'; return; }
  document.getElementById('signatureData').value = croppedSignature();
  button.disabled = true; button.textContent = 'Processando assinatura...'; status.textContent = 'Aguarde enquanto preparamos seu contrato.';
  try {
    const response = await fetch(window.location.href, { method: 'POST', body: new FormData(form) });
    if (!response.ok) { const data = await response.json().catch(() => ({})); throw new Error(data.erro || 'Não foi possível gerar o contrato assinado.'); }
    contractBlob = await response.blob(); contractUrl = URL.createObjectURL(contractBlob);
    document.getElementById('signatureCard').hidden = true; document.getElementById('signSuccess').hidden = false; document.getElementById('signSuccess').scrollIntoView({ behavior: 'smooth', block: 'center' });
    downloadContract();
  } catch (error) { status.textContent = error.message || 'Não foi possível concluir. Tente novamente.'; }
  finally { button.disabled = false; button.textContent = 'Confirmar assinatura'; }
});
document.getElementById('downloadSigned').addEventListener('click', downloadContract);
document.getElementById('shareSigned').addEventListener('click', async () => {
  const help = document.getElementById('shareHelp'); if (!contractBlob) return;
  const file = new File([contractBlob], 'contrato_assinado.docx', { type: contractBlob.type });
  try {
    if (!navigator.share || !navigator.canShare?.({ files: [file] })) { downloadContract(); help.textContent = 'O aparelho não permite compartilhamento direto. O contrato foi baixado.'; return; }
    await navigator.share({ title: 'Contrato odontológico assinado', text: 'Segue o contrato odontológico assinado.', files: [file] }); help.textContent = 'Contrato compartilhado com sucesso.';
  } catch (error) { if (error.name !== 'AbortError') downloadContract(); help.textContent = error.name === 'AbortError' ? 'Compartilhamento cancelado. O contrato continua disponível.' : 'Não foi possível compartilhar diretamente. O contrato foi baixado.'; }
});
