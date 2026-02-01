export default async function handler(req, res) {
  // Healthcheck simples (opcional)
  // Se você abrir /api/whatsapp no navegador sem query, responde ok
  if (req.method === 'GET' && !req.query?.['hub.mode']) {
    return res.status(200).send('OK');
  }

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
      const body = await readJson(req);

      // Estrutura padrão do webhook
      const entry = body?.entry?.[0];
      const change = entry?.changes?.[0];
      const value = change?.value;

      // Pode vir status sem mensagem
      const msg = value?.messages?.[0];
      if (!msg) return res.status(200).send('OK');

      // Evita loop se algum dia vier mensagem "ecoada"
      // (em geral não precisa, mas é seguro)
      if (msg.from && value?.metadata?.display_phone_number) {
        // segue normal
      }

      const from = msg.from; // telefone do usuário (formato: DDI+DDD+numero sem +)
      const type = msg.type;

      let text = '';
      if (type === 'text') {
        text = msg.text?.body?.trim() || '';
      } else if (type === 'button') {
        text = msg.button?.text?.trim() || '[botão]';
      } else if (type === 'interactive') {
        // pode ser list_reply ou button_reply
        const ir = msg.interactive || {};
        text =
          ir?.button_reply?.title ||
          ir?.list_reply?.title ||
          '[interativo]';
      } else {
        text = `[${type}]`;
      }

      console.log('INBOUND:', { from, type, text });

      // resposta simples (eco)
      await sendText(from, `Recebi: ${text}`);

      return res.status(200).send('OK');
    } catch (e) {
      console.error('Webhook error:', e);
      // Meta exige 200 pra não ficar re-tentando infinito
      return res.status(200).send('OK');
    }
  }

  return res.status(405).send('Method Not Allowed');
}

/**
 * Lê o JSON do request (funciona tanto se req.body vier pronto
 * quanto se precisar ler o stream).
 */
async function readJson(req) {
  if (req.body && typeof req.body === 'object') return req.body;

  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);

  const raw = Buffer.concat(chunks).toString('utf8');
  if (!raw) return {};

  try {
    return JSON.parse(raw);
  } catch (e) {
    console.error('Invalid JSON body:', raw);
    return {};
  }
}

async function sendText(to, body) {
  const phoneNumberId = process.env.PHONE_NUMBER_ID;
  const token = process.env.WHATSAPP_TOKEN;

  if (!phoneNumberId) {
    console.error('ENV missing: PHONE_NUMBER_ID');
    return;
  }
  if (!token) {
    console.error('ENV missing: WHATSAPP_TOKEN');
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

  const respText = await resp.text();

  if (!resp.ok) {
    console.error('SendText failed:', resp.status, respText);
  } else {
    console.log('SendText OK:', respText);
  }
}
