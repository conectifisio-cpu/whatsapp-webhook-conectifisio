/**
 * Vercel / Next.js API Route
 * Endpoint:
 *   GET  /api/whatsapp  -> verificação do webhook (challenge)
 *   POST /api/whatsapp  -> recebe mensagens e responde
 */

const GRAPH_API_VERSION = process.env.GRAPH_API_VERSION || 'v19.0';

export default async function handler(req, res) {
  // -------- 1) Verificação do webhook (Meta chama via GET) --------
  if (req.method === 'GET') {
    const mode = req.query['hub.mode'];
    const token = req.query['hub.verify_token'];
    const challenge = req.query['hub.challenge'];

    if (mode === 'subscribe' && token === process.env.VERIFY_TOKEN) {
      return res.status(200).send(challenge);
    }
    return res.status(403).send('Forbidden');
  }

  // -------- 2) Recebimento de eventos (Meta chama via POST) --------
  if (req.method === 'POST') {
    try {
      // Meta manda: { object, entry: [{ changes: [{ value: {...} }] }] }
      const entries = Array.isArray(req.body?.entry) ? req.body.entry : [];

      // Percorre tudo (às vezes vem mais de um change no mesmo POST)
      for (const entry of entries) {
        const changes = Array.isArray(entry?.changes) ? entry.changes : [];

        for (const change of changes) {
          const value = change?.value;

          // 2.1) Se for STATUS (entrega, lido etc.), não precisa responder
          if (value?.statuses?.length) {
            // você pode logar se quiser:
            // console.log('Status event:', value.statuses?.[0]);
            continue;
          }

          // 2.2) Se for MENSAGEM recebida
          const msg = value?.messages?.[0];
          if (!msg) continue;

          const from = msg.from; // wa_id do usuário (ex.: "551197...")
          const type = msg.type;

          let text = '';

          if (type === 'text') {
            text = msg.text?.body?.trim() || '';
          } else if (type === 'interactive') {
            // botão / lista
            const btn = msg.interactive?.button_reply?.title;
            const list = msg.interactive?.list_reply?.title;
            text = (btn || list || '').trim();
          } else {
            text = '';
          }

          // Resposta simples (eco)
          if (text) {
            await sendText(from, `Recebi: ${text}`);
          } else {
            await sendText(from, `Recebi sua mensagem 👍 (tipo: ${type})`);
          }
        }
      }

      // Sempre 200 OK para a Meta não reenviar
      return res.status(200).send('OK');
    } catch (e) {
      console.error('Webhook error:', e);
      // Mesmo em erro, devolve 200 para evitar retry infinito
      return res.status(200).send('OK');
    }
  }

  return res.status(405).send('Method Not Allowed');
}

async function sendText(to, body) {
  const phoneNumberId = process.env.PHONE_NUMBER_ID;
  const token = process.env.WHATSAPP_TOKEN;

  if (!phoneNumberId || !token) {
    console.error('ENV faltando: PHONE_NUMBER_ID ou WHATSAPP_TOKEN');
    return;
  }

  const url = `https://graph.facebook.com/${GRAPH_API_VERSION}/${phoneNumberId}/messages`;

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
    console.error('SendText failed:', resp.status, err);
  }
}
