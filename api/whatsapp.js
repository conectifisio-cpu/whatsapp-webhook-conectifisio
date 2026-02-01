// api/whatsapp.js  (Vercel Serverless Function)

module.exports = async (req, res) => {
  // =========================
  // 1) VERIFICAÇÃO (GET)
  // =========================
  if (req.method === 'GET') {
    const mode = req.query['hub.mode'];
    const token = req.query['hub.verify_token'];
    const challenge = req.query['hub.challenge'];

    // ajuda quando você abre /api/whatsapp sem query
    if (!mode && !token && !challenge) {
      return res.status(200).send('OK');
    }

    if (mode === 'subscribe' && token === process.env.VERIFY_TOKEN) {
      console.log('[WEBHOOK][GET] verified OK');
      return res.status(200).send(challenge);
    }

    console.log('[WEBHOOK][GET] forbidden', { mode, token });
    return res.status(403).send('Forbidden');
  }

  // =========================
  // 2) EVENTOS (POST)
  // =========================
  if (req.method === 'POST') {
    try {
      const body = normalizeBody(req.body);
      console.log('[WEBHOOK][POST] received');

      // Pode vir em lote (várias entries/changes)
      const entries = Array.isArray(body?.entry) ? body.entry : [];
      for (const entry of entries) {
        const changes = Array.isArray(entry?.changes) ? entry.changes : [];
        for (const change of changes) {
          const value = change?.value;

          // ===== IMPORTANTÍSSIMO =====
          // Se o evento não for do seu PHONE_NUMBER_ID, ignore.
          // Isso evita erro no "teste do painel" (payload fake).
          const incomingPhoneNumberId = value?.metadata?.phone_number_id;
          const myPhoneNumberId = String(process.env.PHONE_NUMBER_ID || '');

          if (incomingPhoneNumberId && myPhoneNumberId) {
            if (String(incomingPhoneNumberId) !== myPhoneNumberId) {
              console.log(
                `[WEBHOOK] ignoring event for phone_number_id=${incomingPhoneNumberId} (mine=${myPhoneNumberId})`
              );
              continue;
            }
          }

          // Mensagens recebidas
          const messages = Array.isArray(value?.messages) ? value.messages : [];
          for (const msg of messages) {
            const from = msg?.from; // wa_id (sem +)
            const type = msg?.type;

            if (!from || !type) continue;

            let text = '';

            if (type === 'text') {
              text = (msg.text?.body || '').trim();
            } else if (type === 'interactive') {
              // caso você use botões/listas no futuro
              const i = msg.interactive || {};
              text =
                i?.button_reply?.title ||
                i?.list_reply?.title ||
                '[interactive]';
            } else {
              text = `[${type}]`;
            }

            console.log(
              `[WEBHOOK][MSG] from: ${from} type: ${type} text: ${text || '(empty)'}`
            );

            // Resposta simples (eco)
            await sendText(from, `Recebi: ${text || '(sem texto)'}`);
          }
        }
      }

      return res.status(200).send('OK');
    } catch (e) {
      console.error('[WEBHOOK] error:', e);
      // Meta recomenda sempre 200 para não re-tentar sem parar
      return res.status(200).send('OK');
    }
  }

  res.setHeader('Allow', 'GET, POST');
  return res.status(405).send('Method Not Allowed');
};

// ----------------------------
// Helpers
// ----------------------------
function normalizeBody(body) {
  // Em alguns casos pode vir como string
  if (typeof body === 'string') {
    try {
      return JSON.parse(body);
    } catch {
      return {};
    }
  }
  return body || {};
}

async function sendText(to, body) {
  const phoneNumberId = process.env.PHONE_NUMBER_ID;
  const token = process.env.WHATSAPP_TOKEN;

  if (!phoneNumberId) {
    console.error('[SEND] missing PHONE_NUMBER_ID env var');
    return;
  }
  if (!token) {
    console.error('[SEND] missing WHATSAPP_TOKEN env var');
    return;
  }

  const url = `https://graph.facebook.com/v19.0/${phoneNumberId}/messages`;

  const payload = {
    messaging_product: 'whatsapp',
    to,
    type: 'text',
    text: { body }
  };

  const resp = await fetch(url, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify(payload)
  });

  if (!resp.ok) {
    const err = await resp.text();
    console.error('[SEND] failed:', resp.status, err);
    return;
  }

  const data = await resp.json().catch(() => null);
  console.log('[SEND] ok', data?.messages?.[0]?.id ? `msg_id=${data.messages[0].id}` : '');
}
