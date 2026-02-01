// /api/whatsapp.js

export default async function handler(req, res) {
  // evita cache atrapalhar o GET do challenge
  res.setHeader("Cache-Control", "no-store");

  // ==========
  // 1) GET - verificação do webhook (challenge)
  // ==========
  if (req.method === "GET") {
    const mode = req.query["hub.mode"];
    const token = req.query["hub.verify_token"];
    const challenge = req.query["hub.challenge"];

    if (mode === "subscribe" && token === process.env.VERIFY_TOKEN) {
      return res.status(200).send(challenge);
    }
    return res.status(403).send("Forbidden");
  }

  // ==========
  // 2) POST - recebimento de eventos (mensagens)
  // ==========
  if (req.method === "POST") {
    try {
      const body = await getJsonBody(req);

      // Logs úteis (aparecem em Vercel > Logs)
      // console.log("Webhook POST body:", JSON.stringify(body));

      const entry = body?.entry?.[0];
      const change = entry?.changes?.[0];
      const value = change?.value;

      // Pode vir status, messages, etc.
      const msg = value?.messages?.[0];
      if (!msg) return res.status(200).send("OK");

      const from = msg.from; // ex: 5511971904516
      const text = extractText(msg);

      console.log("Mensagem recebida:", { from, type: msg.type, text });

      // Se não for texto/interactive, só confirma OK
      if (!from || !text) return res.status(200).send("OK");

      // Resposta simples (eco)
      await sendText(from, `Recebi: ${text}`);

      return res.status(200).send("OK");
    } catch (e) {
      console.error("Webhook error:", e);
      // Importante: sempre 200 para o Meta não ficar re-tentando em loop
      return res.status(200).send("OK");
    }
  }

  return res.status(405).send("Method Not Allowed");
}

// -------------------------------------------------------------

async function sendText(to, body) {
  const phoneNumberId = process.env.PHONE_NUMBER_ID;
  const token = process.env.WHATSAPP_TOKEN;

  if (!phoneNumberId) {
    console.error("Faltou env PHONE_NUMBER_ID");
    return;
  }
  if (!token) {
    console.error("Faltou env WHATSAPP_TOKEN");
    return;
  }

  const url = `https://graph.facebook.com/v19.0/${phoneNumberId}/messages`;

  const payload = {
    messaging_product: "whatsapp",
    to,
    type: "text",
    text: { body },
  };

  const resp = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  if (!resp.ok) {
    const err = await resp.text();
    console.error("SendText failed:", resp.status, err);
  } else {
    // opcional: logar sucesso
    // const ok = await resp.json();
    // console.log("SendText ok:", ok);
  }
}

// Lê JSON mesmo quando req.body vier vazio/string (mais robusto no Vercel)
async function getJsonBody(req) {
  if (req.body && typeof req.body === "object") return req.body;

  let raw = "";
  await new Promise((resolve, reject) => {
    req.on("data", (chunk) => (raw += chunk));
    req.on("end", resolve);
    req.on("error", reject);
  });

  if (!raw) return {};
  try {
    return JSON.parse(raw);
  } catch {
    return {};
  }
}

// Extrai texto de vários tipos de mensagem
function extractText(msg) {
  // texto normal
  if (msg?.text?.body) return String(msg.text.body).trim();

  // botões/listas (interactive)
  if (msg?.interactive?.button_reply?.title)
    return String(msg.interactive.button_reply.title).trim();

  if (msg?.interactive?.list_reply?.title)
    return String(msg.interactive.list_reply.title).trim();

  return "";
}
