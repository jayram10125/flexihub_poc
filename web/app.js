const $ = s => document.querySelector(s);
let token = sessionStorage.getItem('portway-token');
let user = null;
let overview = null;
let demoRequester = null;
let registerMode = false;
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, method='GET', body) {
  const response = await fetch('/api' + path, {method, headers:{'Content-Type':'application/json', ...(token ? {'Authorization':'Bearer '+token} : {})}, body:body ? JSON.stringify(body) : undefined});
  const data = await response.json().catch(()=>({}));
  if (!response.ok) throw new Error(data.detail?.message || data.detail || 'Request failed');
  return data;
}

function notice(message, isError=false) {
  const el = $('#notice');
  el.textContent = message;
  el.hidden = !message;
  el.classList.toggle('error', isError);
}

function showAuth(show=true) { $('#auth-modal').classList.toggle('hidden', !show); }
function showSetup(show=true) { $('#setup-server-url').textContent = window.location.origin; $('#setup-modal').classList.toggle('hidden', !show); }

function render() {
  if (!overview) return;
  const {machines, devices} = overview;
  $('#nav-count').textContent = devices.length;
  $('#computer-count').textContent = machines.length;
  $('#profile-email').textContent = user?.email || 'Sign in to continue';
  $('#profile-name').textContent = user ? 'My workspace' : 'Your workspace';
  $('#avatar').textContent = (user?.email || 'U')[0].toUpperCase();
  const byId = Object.fromEntries(machines.map(m => [m.id,m]));
  $('#device-list').innerHTML = devices.length ? devices.map(d => {
    const demo = d.kind === 'demo';
    const status = d.status === 'connected' ? 'In use' : d.status === 'offline' ? 'Offline' : 'Available';
    const action = d.session_id ? `<button class="card-action" data-disconnect="${escapeHtml(d.session_id)}">Disconnect →</button>` : `<button class="card-action" data-connect="${escapeHtml(d.id)}" ${d.status === 'offline' ? 'disabled' : ''}>Connect →</button>`;
    return `<article class="device-card"><div class="device-top"><span class="device-icon">▣</span><span class="badge ${escapeHtml(d.status)}">${status}</span></div><h3>${escapeHtml(d.name)}</h3><div class="device-meta">${demo ? 'DEMO DEVICE · NO USB ATTACH' : 'USB MASS STORAGE'}${d.vid ? ' · '+escapeHtml(d.vid)+':'+escapeHtml(d.pid) : ''}</div><div class="device-footer"><span class="device-host">On <strong>${escapeHtml(byId[d.machine_id]?.name || 'Unknown PC')}</strong></span>${action}</div></article>`;
  }).join('') : '<div class="empty-card"><strong>No devices yet</strong><p>Run a Windows connector, or select Explore demo to preview the flow.</p></div>';
  $('#computer-list').innerHTML = machines.length ? machines.map(m => `<article class="computer-card"><span class="computer-icon">▤</span><div class="computer-name"><strong>${escapeHtml(m.name)}</strong><small>${escapeHtml(m.os)}${m.kind === 'demo' ? ' · Demo machine' : ''}</small></div><span class="badge ${m.online ? 'available' : 'offline'}">${m.online ? 'Online' : 'Offline'}</span></article>`).join('') : '<div class="empty-card"><strong>No computers connected</strong><p>Run the connector on both Windows PCs with the same account.</p></div>';
}

async function refresh() {
  if (!token) return;
  try { overview = await api('/overview'); user = overview.user; render(); }
  catch (error) { if (/sign in|expired/i.test(error.message)) { token=null; sessionStorage.removeItem('portway-token'); showAuth(); } else notice(error.message,true); }
}

$('#auth-form').addEventListener('submit', async event => {
  event.preventDefault();
  $('#auth-error').textContent = '';
  const data = Object.fromEntries(new FormData(event.target));
  try {
    const result = await api(registerMode ? '/auth/register' : '/auth/login','POST',data);
    token = result.token; user = result.user; sessionStorage.setItem('portway-token',token);
    showAuth(false); await refresh();
  } catch(error) { $('#auth-error').textContent = error.message; }
});
$('#auth-switch').addEventListener('click', () => { registerMode=!registerMode; $('#auth-title').textContent=registerMode?'Create your account':'Sign in to continue'; $('#auth-switch').textContent=registerMode?'Sign in instead':'Create an account'; $('#auth-form button[type=submit]').firstChild.textContent=registerMode?'Create account ':'Sign in '; $('#auth-error').textContent=''; });
$('#close-auth').addEventListener('click', () => { if (token) showAuth(false); });
$('#logout').addEventListener('click', async () => { try { await api('/auth/logout','POST'); } catch {} token=null;user=null;overview=null;sessionStorage.removeItem('portway-token');showAuth();notice(''); });
$('#add-computer').addEventListener('click', () => showSetup());
$('#close-setup').addEventListener('click', () => showSetup(false));
$('#setup-done').addEventListener('click', () => showSetup(false));
$('#guide-link').addEventListener('click', event => { event.preventDefault();showSetup(); });
$('#refresh').addEventListener('click', refresh);
$('#refresh-computers').addEventListener('click', refresh);
$('#load-demo').addEventListener('click', async () => { if(!token){showAuth();return;} try { const result=await api('/demo','POST');demoRequester=result.requester_machine_id;notice('Demo devices loaded. Demo Connect reserves a sample device only; it does not mount USB storage.');await refresh(); } catch(error) {notice(error.message,true);} });
$('#device-list').addEventListener('click', async event => {
  const connect = event.target.closest('[data-connect]');
  const disconnect = event.target.closest('[data-disconnect]');
  if (!connect && !disconnect) return;
  try {
    if (disconnect) { await api('/sessions/'+encodeURIComponent(disconnect.dataset.disconnect)+'/disconnect','POST'); notice('Demo session released.'); }
    else {
      const device = overview.devices.find(d => d.id === connect.dataset.connect);
      const requester = device.kind === 'demo' ? (demoRequester || overview.machines.find(m => m.kind === 'demo' && m.id !== device.machine_id)?.id) : overview.machines.find(m => m.id !== device.machine_id && m.online && m.kind === 'connector')?.id;
      if (!requester) throw new Error('A second online computer is needed to connect.');
      const result = await api('/sessions','POST',{device_id:device.id, requester_machine_id:requester});
      notice(result.state === 'demo-connected' ? 'Demo session connected. No USB device was mounted.' : 'Session started.');
    }
    await refresh();
  } catch(error) {notice(error.message,true);}
});
if (token) { showAuth(false); refresh(); } else showAuth();
setInterval(refresh,15000);
