export default async function handler(req, res) {
  // 1) Verificação do webhook (Meta chama via GET)
  if (req.method === 'GET') {
    const mode = req.query['hub.mode'];
    const token = req.query['hub.verify_token'];
    const challenge = req.query['hub.challenge'];

    if (mode === 'subscribe' && token === process.env.VERIFY_TOKEN) {
      return res.status(200).send(challenge);
    }
    return res.status(403).send('Forbidden');
  }

  // 2) Recebimento de eventos (Meta chama via POST)
  if (req.method === 'POST') {
    try {
      const entry = req.body?.entry?.[0];
      const change = entry?.changes?.[0];
      const value = change?.value;

      const msg = value?.messages?.[0];
      if (!msg) return res.status(200).send('OK');

      const from = msg.from;
      const text = msg.text?.body?.trim() || '';

      // resposta simples (eco)
      await sendText(from, `Recebi: ${text}`);

      return res.status(200).send('OK');
    } catch (e) {
      console.error('Webhook error:', e);
      return res.status(200).send('OK');
    }
  }

  return res.status(405).send('Method Not Allowed');
}

async function sendText(to, body) {
  const phoneNumberId = process.env.PHONE_NUMBER_ID;
  const token = process.env.WHATSAPP_TOKEN;

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
    console.error('SendText failed:', resp.status, err);
  }
}
