<script>
// ---- the account: only Stentor Cloud needs one. Signed out, dictation works with This computer or your own key ----
const signIn = Object.assign(document.createElement('button'), { className: 'btn small primary', id: 'signIn', textContent: 'Sign in with Google' });
function renderAccount(s) {
  const a = s.account;
  $('acctGroup').hidden = !a;
  if (a) { $('acctName').textContent = a.name || a.email; $('acctMail').textContent = a.name ? a.email : ''; }
  const on = !!(a && a.plan === 'plus'); // the features come with Stentor Cloud alone
  $('premGroup').classList.toggle('on', on);
  $('premLine').textContent = on ? 'Included in your Stentor Cloud subscription. The text involved goes through the Stentor server.' : 'These come with Stentor Cloud.';
  document.querySelectorAll('.tag.prem').forEach(t => { t.textContent = 'Stentor Cloud'; });
  if (on) renderPremium(s);
  // The clean-up drops fillers and writes spoken punctuation itself, so those manual switches step aside while it runs.
  const cleaning = on && s.config.plus_clean && !s.config.live_text;
  $('remove_fillers').closest('.row').hidden = cleaning;
  $('fillerRow').hidden = cleaning || !s.config.remove_fillers;
  $('spoken_punctuation').closest('.item').hidden = cleaning;
  if (on && s.config.live_text) renderLiveHint(s);
}
// Typed live, the text can't be cleaned up at the end, so the two can't be on together.
function renderLiveHint(s) {
  const c = s.config, hint = $('liveHint');
  if (c.plus_clean) hint.textContent = 'Clean up what you said is on, so the text still comes when you finish. Turn it off to use this.';
  else if (!c.post_hook.trim() && (s.platform === 'linux' || c.hotkey_mode !== 'hold')) hint.textContent += ' Clean up what you said stays off with this.';
}
pageHooks.renders.push(renderAccount);

// ---- Stentor Cloud: what it includes, and one way in: sign in, subscribe (while it is on sale), or manage ----
const day = iso => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || ''); return m ? new Date(+m[1], m[2] - 1, +m[3]).toLocaleDateString('en', { day: 'numeric', month: 'long' }) : ''; };
const minutes = sec => { const m = (Number(sec) || 0) / 60; return m < 10 && m % 1 ? m.toFixed(1).replace(/\.0$/, '') : String(Math.floor(m)); };
// Where a non-subscriber stands: 'signin', 'buy' (on sale), 'soon' (not on sale yet), or 'plus'.
const cloudStep = s => { const a = s.account; return !a ? 'signin' : a.plan === 'plus' ? 'plus' : a.sale ? 'buy' : 'soon'; };
const CLOUD_ASK = { signin: 'Sign in to use Stentor Cloud.', buy: 'Subscribe to use Stentor Cloud.', soon: 'Stentor Cloud is opening soon. Until then, dictate with This computer or your own key.' };
function renderPlan(s) {
  const a = s.account, step = cloudStep(s), b = (a && a.billing) || {}, cloud = (a && a.cloud) || {};
  const plus = step === 'plus', fix = b.status === 'past_due'; // a subscription waiting for its payment is fixed where it is managed
  $('planPerks').hidden = plus;
  $('planLine').textContent = plus ? 'Cloud transcription, the clean-up and edit by voice, with no API key of your own.'
    : 'The easiest way to dictate: sign in, subscribe, and talk. Free alternatives stay under Where: This computer, or your own key.';
  $('planName').textContent = plus ? 'Subscribed' : step === 'soon' ? 'Opening soon' : 'Recommended';
  $('planBill').textContent = b.pending ? 'Payment processing. Stentor Cloud turns on as soon as it goes through.' : fix ? 'The last payment didn\'t go through. Update it in Manage subscription.'
    : plus && b.cancel_at_period_end && day(b.renews_at) ? `Ends on ${day(b.renews_at)}. It doesn't renew.`
    : plus && day(b.renews_at) ? `Renews on ${day(b.renews_at)}.`
    : !plus && b.status ? 'Your Stentor Cloud subscription has ended.'
    : step === 'signin' ? 'Sign in with Google, then subscribe.' : step === 'soon' ? 'It is not open for subscriptions yet. Until then, dictate with This computer or your own key.'
    : 'Subscribe in your browser, then dictate here with no key.';
  const btn = $('planBtn');
  btn.dataset.kind = plus || fix ? 'manage' : 'upgrade';
  btn.textContent = plus || fix ? 'Manage subscription' : 'Subscribe';
  btn.classList.toggle('primary', !plus && !fix);
  btn.hidden = step === 'signin' || (step === 'soon' && !fix) || (!!b.pending && !plus); // nothing to buy while a payment is on its way
  if (step === 'signin' && signIn.parentNode !== $('planRow')) $('planRow').append(signIn);
  signIn.hidden = step !== 'signin';
  if (!plus) return;
  const limit = Number(cloud.limit) || 0, used = Math.min(Number(cloud.used) || 0, limit || Infinity), full = limit > 0 && used >= limit;
  $('usageMeter').classList.toggle('full', full);
  $('usageMeter').setAttribute('aria-valuemax', String(limit)); $('usageMeter').setAttribute('aria-valuenow', String(used));
  $('usageBar').style.width = limit ? `${Math.round(used / limit * 100)}%` : '0';
  const resets = day(cloud.resets);
  $('usageText').textContent = !limit ? 'This month\'s numbers come with the next check.'
    : full ? `All ${minutes(limit)} minutes are used. Your other transcription choice takes over${resets ? ` until ${resets}` : ''}.`
    : `${minutes(used)} of ${minutes(limit)} minutes used${resets ? `. Starts again on ${resets}` : ''}.`;
  // What takes a dictation the cloud can't: the service you have a key for, then the offline engine.
  const label = ((s.providers[s.config.provider] || {}).label || '').replace(/ \(.*/, '');
  const own = s.config.provider !== 'local' && label && (s.key_hint || s.key_from_env) ? label + ' with your own key' : '';
  const here = s.local_installed && (s.config.provider === 'local' || s.config.local_fallback) ? 'the offline engine on this computer' : '';
  $('cloudBackup').textContent = own || here
    ? `If the cloud can't be reached, ${[own, here].filter(Boolean).join(', then ')} takes the dictation.`
    : 'If the cloud can\'t be reached, a dictation fails. To have a backup, save a key under Your own key or install the offline engine under This computer, then come back here.';
}
// The month's cloud time used up with nothing else to take a dictation: say that, not "paste an API key".
pageHooks.blockers.push(s => {
  const a = s.account, c = s.config, cloud = (a && a.cloud) || {};
  if (!a || a.plan !== 'plus' || !c.plus_cloud || !(Number(cloud.limit) > 0) || Number(cloud.used) < Number(cloud.limit)) return null;
  if (c.provider === 'local' ? s.local_installed : s.key_hint || s.key_from_env) return null; // that one takes over
  const resets = day(cloud.resets);
  return { title: 'This month\'s cloud time is used up.', lede: `Stentor Cloud starts again${resets ? ` on ${resets}` : ' next month'}. Until then, pick Your own key or This computer under Transcription below.` };
});
// A new install starts on This computer without its engine: offer the two ways to start, Stentor Cloud first.
const startBox = document.createElement('div');
startBox.className = 'start-choice';
const cloudStart = Object.assign(document.createElement('button'), { className: 'btn primary', type: 'button', textContent: 'Use Stentor Cloud' });
cloudStart.addEventListener('click', () => { const b = document.querySelector('#where button[data-k="cloud"]'); if (b) b.click(); }); // as if picked under Where
pageHooks.blockers.push(s => {
  const c = s.config;
  if (!s.running || s.problem || s.extension_transcribes || c.provider !== 'local' || s.local_installed) return null;
  if (cloudStep(s) === 'plus' && c.plus_cloud) return null;
  const button = !s.offline.can_install; // otherwise the engine is installed from a terminal
  const command = Object.assign(document.createElement('code'), { className: 'cmd', textContent: s.offline.command });
  startBox.replaceChildren(cloudStart, button ? offline.box : command);
  return {
    title: 'Choose how to start dictating.',
    lede: 'Stentor Cloud is the easiest: nothing to install, no key, and the most accurate. Or dictate free on this computer: '
      + (button ? 'the offline model is a download of a few hundred MB.' : 'install the offline model by running the command below in a terminal, then reload this page.'),
    button: startBox,
  };
});
// Stentor Cloud is the first answer to "Where" in Transcription, always there and marked as recommended. Without a
// subscription, picking it leads to the Stentor Cloud section: sign in, subscribe, or "opening soon".
const cloudPanel = $('cloudPanel');
cloudPanel.remove(); // the page puts it under the choice while the cloud is on
pageHooks.places.push({
  id: 'cloud', label: 'Stentor Cloud', panel: cloudPanel,
  hint: 'Recommended. Your recordings go through the Stentor server to be turned into text: no key, nothing to install, and the most accurate.',
  shown: () => true,
  on: s => cloudStep(s) === 'plus' && !!s.config.plus_cloud,
  pick: s => {
    const step = cloudStep(s);
    if (step === 'plus') return { plus_cloud: true };
    $('planGroup').scrollIntoView({ block: 'center' }); $('planMsg').className = 'note'; $('planMsg').textContent = CLOUD_ASK[step];
    (step === 'signin' ? signIn : step === 'buy' ? $('planBtn') : $('planGroup')).focus({ preventScroll: true });
    return null;
  },
  leave: () => ({ plus_cloud: false }),
});
// "Recommended" on the Stentor Cloud button: the page draws the buttons again on every render, so this follows it.
pageHooks.renders.push(s => {
  const b = document.querySelector('#where button[data-k="cloud"]');
  if (b && !b.querySelector('.rec') && cloudStep(s) !== 'plus') b.append(Object.assign(document.createElement('span'), { className: 'rec', textContent: 'Recommended' }));
});
pageHooks.renders.push(renderPlan);
$('premGroup').before($('planGroup')); // Stentor Cloud comes first, as the features below follow from it
let billingOpen = false, lastCheck = Date.now();
$('planBtn').onclick = async () => {
  const btn = $('planBtn'), msg = $('planMsg'), upgrade = btn.dataset.kind === 'upgrade';
  btn.disabled = true; msg.className = 'note wait'; msg.textContent = 'Opening your browser…';
  try {
    S = await api(upgrade ? '/api/plan/upgrade' : '/api/plan/manage', {}); render(S);
    billingOpen = true; msg.className = 'note';
    msg.textContent = upgrade ? 'Finish in your browser. This page shows Stentor Cloud when you come back.' : 'Your subscription is open in your browser.';
  } catch (e) { msg.className = 'note bad'; msg.textContent = e.message; }
  finally { btn.disabled = false; }
};
// Back from the browser, or every few minutes while the page is open: ask the server, cheaply.
async function recheck(min) {
  if (!S || !S.account || document.hidden || Date.now() - lastCheck < min) return;
  lastCheck = Date.now();
  const was = S.account.plan;
  try { S = await api('/api/account/check', {}); render(S); } catch { return; }
  if (billingOpen && S.account && S.account.plan !== was) { billingOpen = false; $('planMsg').className = 'note ok'; $('planMsg').textContent = S.account.plan === 'plus' ? 'Stentor Cloud is on. Thank you!' : 'Stentor Cloud has ended.'; }
}
addEventListener('focus', () => recheck(billingOpen ? 3000 : 30000));
document.addEventListener('visibilitychange', () => recheck(billingOpen ? 3000 : 30000));
setInterval(() => recheck(4 * 60000), 60000);

// ---- sharing recordings: the card until it's answered, then a switch ----
function renderSharing(s) {
  const share = s.sharing;
  $('sharingCard').hidden = !s.account || !share.ask;
  $('sharingGroup').hidden = !s.account || !share.answer && !share.on || share.ask;
  $('sharingOn').checked = share.answer === 'yes'; $('sharingOn').disabled = !share.on;
  $('cardInfo').href = $('sharingInfo').href = share.page;
}
pageHooks.renders.push(renderSharing);
document.querySelector('section.hero').after($('sharingCard')); // the sections arrive further down the page
async function sharing(path, msg) {
  try { S = await api(path, {}); msg.className = 'note'; msg.textContent = ''; render(S); return S; }
  catch (e) { msg.className = 'note bad'; msg.textContent = e.message; render(S); }
}
$('cardYes').onclick = () => sharing('/api/sharing/yes', $('cardMsg'));
$('cardNo').onclick = () => sharing('/api/sharing/no', $('cardMsg'));
$('sharingOn').addEventListener('change', () => sharing($('sharingOn').checked ? '/api/sharing/yes' : '/api/sharing/no', $('sharingMsg')));
$('sharingDelete').onclick = async () => {
  if (!confirm('Delete everything you shared, and turn sharing off?')) return;
  const s = await sharing('/api/sharing/delete', $('sharingMsg'));
  if (s) { $('sharingMsg').className = 'note ok'; $('sharingMsg').textContent = 'Your shared recordings and text are deleted.'; }
};

// ---- the Stentor Cloud features: edit by voice, clean up what you said, a dictionary that learns ----
function renderPremium(s) {
  const c = s.config;
  if (recording !== 'edit_hotkey') keycaps(c.edit_hotkey ? c.edit_hotkey.split('+') : [], '', $('editKeys'));
  $('editOff').hidden = !c.edit_hotkey || recording === 'edit_hotkey';
  if (recording !== 'edit_hotkey') $('recEdit').textContent = c.edit_hotkey ? 'Change' : 'Choose a shortcut';
  $('plus_clean').checked = c.plus_clean; $('premium_learn').checked = c.premium_learn;
  renderClean(s);
  const terms = s.premium.terms, fixes = Object.entries(c.learned_fixes), box = $('learnRows');
  $('learnBox').hidden = !c.premium_learn && !terms.length && !fixes.length;
  $('learnEmpty').hidden = !!(terms.length || fixes.length); $('forget').hidden = !$('learnEmpty').hidden;
  keepFocus(box, () => { box.textContent = '';
  const row = (k, cells, drop) => {
    const r = document.createElement('div'); r.className = 'pair'; r.setAttribute('role', 'listitem');
    for (const [cls, text] of cells) r.append(Object.assign(document.createElement('span'), { className: cls, textContent: text, dir: cls ? '' : 'auto' }));
    const x = Object.assign(document.createElement('button'), { className: 'btn icon', innerHTML: ICON_X, title: 'Remove' });
    x.dataset.k = k; x.setAttribute('aria-label', `Remove ${cells.map(c => c[1]).join(' ')}`);
    x.onclick = () => { const next = r.nextElementSibling || r.previousElementSibling; if (next) next.querySelector('button').focus(); drop(); };
    r.append(x); box.append(r);
  };
  for (const [heard, written] of fixes) row('f:' + heard, [['', heard], ['to', '→'], ['', written || '(deleted)']], () => {
    const v = { ...S.config.learned_fixes }; delete v[heard]; save({ learned_fixes: v }); });
  for (const term of terms) row('t:' + term, [['', term]], () => save({ learned_terms: S.premium.terms.filter(t => t !== term).join(', ') }));
  });
}
// The clean-up's model menu, and what the last dictation's clean-up did: which model, how long, or why it didn't.
function renderClean(s) {
  const c = s.config, sel = $('clean_model'), models = (s.clean && s.clean.models) || {};
  $('cleanBox').hidden = !c.plus_clean;
  if (sel.options.length !== Object.keys(models).length) {
    sel.textContent = '';
    for (const [id, name] of Object.entries(models)) sel.append(new Option(name, id));
  }
  if (document.activeElement !== sel) sel.value = c.clean_model;
  const last = s.clean && s.clean.last, name = last && (models[last.model] || last.model || 'the server');
  $('cleanLast').textContent = !last ? 'No dictation cleaned up yet since the app started.'
    : !last.ok ? `The last dictation came as heard: ${last.why}`
    : last.changed ? `The last dictation was cleaned up by ${name} in ${last.seconds.toFixed(1)} s. History shows what was heard.`
    : `The last dictation needed no change (${name}, ${last.seconds.toFixed(1)} s).`;
}
// After a click on the "Stentor Cloud is on" notice: the controls open one by one, with arrows at each.
function revealPremium() {
  const group = $('premGroup');
  history.replaceState(null, '', location.pathname + location.search);
  if (!group.classList.contains('on')) return;
  group.classList.add('reveal');
  for (const c of [$('recEdit'), $('plus_clean'), $('premium_learn')]) {
    const arrows = document.createElement('span');
    arrows.className = 'arrows';
    arrows.setAttribute('aria-hidden', 'true');
    arrows.innerHTML = '<i></i><i></i><i></i>';
    c.before(arrows);
    c.addEventListener('click', () => arrows.remove(), { once: true });
    c.classList.add('try');
  }
}
REC.edit_hotkey = { keys: 'editKeys', btn: 'recEdit', msg: 'editMsg' };
$('recEdit').onclick = () => { startRec('edit_hotkey'); if (recording === 'edit_hotkey') $('editOff').hidden = true; };
for (const id of ['plus_clean', 'premium_learn']) $(id).addEventListener('change', () => save({ [id]: $(id).checked }));
$('clean_model').addEventListener('change', () => save({ clean_model: $('clean_model').value }));
$('editOff').onclick = () => save({ edit_hotkey: '' });
$('forget').onclick = () => { if (confirm('Forget every word and fix the dictionary learned?')) save({ learned_terms: '', learned_fixes: {} }); };

signIn.onclick = async () => {
  signIn.disabled = true;
  $('planMsg').className = 'note wait'; $('planMsg').textContent = 'Opening Google…';
  try { location.href = (await api('/api/account/signin', {})).url; } // Google sends the browser back to this page
  catch (e) { $('planMsg').className = 'note bad'; $('planMsg').textContent = e.message; signIn.disabled = false; }
};
$('signOut').onclick = async () => {
  $('signOut').disabled = true;
  try { S = await api('/api/account/signout', {}); $('planMsg').textContent = ''; render(S); }
  catch (e) { saveState('failed', e.message); }
  finally { $('signOut').disabled = false; }
};
// Stentor Cloud may have started or ended (or the account been blocked) since the last check
pageHooks.starts.push(() => {
  const check = S && S.account ? api('/api/account/check', {}).then(s => { S = s; render(s); }).catch(() => {}) : Promise.resolve();
  if (location.hash === '#sharingCard') check.then(() => { $('sharingCard').scrollIntoView({ block: 'center' }); history.replaceState(null, '', location.pathname + location.search); });
  if (location.hash === '#premGroup') { $('premGroup').scrollIntoView({ block: 'start' }); check.then(revealPremium); }
});
</script>
