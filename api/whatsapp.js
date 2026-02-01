// api/whatsapp.js

const GRAPH_VERSION = process.env.GRAPH_VERSION || 'v19.0';

export default async function handler(req, res) {
  try {
    // --- LOG básico (ajuda MUITO no Vercel Logs) ---
    console.log('[WEBHOOK]', req.method, req.url);

    // 1) Verificação do webhook (GET)
    if (req.method === 'GET') {
      const mode = req.query?.['hub.mode'];
      const token = req.query?.['hub.verify_token'];
      const challenge = req.query?.['hub.challenge'];

      console.log('[VERIFY]', { mode, tokenReceived: token, hasChallenge: !!challenge });

      if (mode === 'subscribe' && token === process.env.VERIFY_TOKEN) {
        return res.status(200).send(challenge);
      }
      return res.status(403).send('Forbidden');
    }

    // 2) Recebimento de eventos (POST)
    if (req.method === 'POST') {
      // Em alguns casos o body pode vir como string
      const body = typeof req.body === 'string' ? safeJsonParse(req.body) : req.body;

      // Log resumido
      console.log('[POST] keys:', Object.keys(body || {}));

      const entry = body?.entry?.[0];
      const change = entry?.changes?.[0];
      const value = change?.value;

      // Status (entrega/leitura) — não é mensagem do usuário
      if (value?.statuses?.length) {
        console.log('[STATUS]', value.statuses[0]);
        return res.status(200).send('OK');
      }

      const msg = value?.messages?.[0];
      if (!msg) {
        // Meta costuma mandar eventos que não são "messages"
        return res.status(200).send('OK');
      }

      const from = msg.from; // telefone do usuário (sem +)
      const text = msg.text?.body?.trim() || '';

      console.log('[MESSAGE]', { from, text });

      // Resposta simples (eco)
      await sendText(from, `Recebi: ${text}`);

      return res.status(200).send('OK');
    }

    return res.status(405).send('Method Not Allowed');
  } catch (e) {
    console.error('Webhook error:', e);
    // Sempre devolve 200 pro Meta não ficar reenviando infinito
    return res.status(200).send('OK');
  }
}

function safeJsonParse(str) {
  try {
    return JSON.parse(str);
  } catch {
    return null;
  }
}

async function sendText(to, body) {
  const phoneNumberId = process.env.PHONE_NUMBER_ID;
  const token = process.env.WHATSAPP_TOKEN;

  if (!phoneNumberId || !token) {
    console.error('[SEND] Missing env vars:', {
      hasPhoneNumberId: !!phoneNumberId,
      hasToken: !!token
    });
    return;
  }

  const url = `https://graph.facebook.com/${GRAPH_VERSION}/${phoneNumberId}/messages`;

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
  } else {
    const ok = await resp.json().catch(() => ({}));
    console.log('[SEND] ok:', ok?.messages?.[0] || ok);
  }
}
