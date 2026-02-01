// api/whatsapp.js

export default async function handler(req, res) {
  // Healthcheck simples (opcional)
  // Se você abrir /api/whatsapp sem query, ele responde OK.
  if (req.method === "GET" && !req.query?.["hub.mode"]) {
    return res.status(200).send("OK");
  }

  // 1) Verificação do webhook (Meta chama via GET)
  if (req.method === "GET") {
    const mode = req.query["hub.mode"];
    const token = req.query["hub.verify_token"];
    const challenge = req.query["hub.challenge"];

    if (mode === "subscribe" && token === process.env.VERIFY_TOKEN) {
      return res.status(200).send(challenge);
    }
    return res.status(403).send("Forbidden");
  }

  // 2) Recebimento de eventos (Meta chama via POST)
  if (req.method === "POST") {
    try {
      const body = parseBody(req);

      const entry = body?.entry?.[0];
      const change = entry?.changes?.[0];
      const value = change?.value;

      // Quando não é mensagem (ex: status), só responde OK
      const msg = value?.messages?.[0];
      if (!msg) return res.status(200).send("OK");

      const from = msg.from; // ex: "5511971904516" (sem +)
      const type = msg.type;

      let text = "";
      if (type === "text") text = msg.text?.body?.trim() || "";
      else text = `[${type}]`;

      // Debug básico no log do Vercel (Observability / Logs)
      console.log("Incoming message:", { from, type, text });

      // resposta simples (eco)
      await sendText(from, `Recebi: ${text}`);

      return res.status(200).send("OK");
    } catch (e) {
      console.error("Webhook error:", e);
      // Importante responder 200 para a Meta não ficar re-tentando sem parar
      return res.status(200).send("OK");
    }
  }

  return res.status(405).send("Method Not Allowed");
}

function parseBody(req) {
  // Dependendo do runtime, o body pode vir como objeto ou string
  if (!req.body) return {};
  if (typeof req.body === "object") return req.body;

  try {
    return JSON.parse(req.body);
  } catch {
    return {};
  }
}

async function sendText(to, body) {
  const phoneNumberId = process.env.PHONE_NUMBER_ID;
  const token = process.env.WHATSAPP_TOKEN;

  if (!phoneNumberId || !token) {
    console.error("Missing env vars:", {
      PHONE_NUMBER_ID: !!phoneNumberId,
      WHATSAPP_TOKEN: !!token,
    });
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

  const txt = await resp.text();

  if (!resp.ok) {
    console.error("SendText failed:", resp.status, txt);
  } else {
    console.log("SendText OK:", txt);
  }
}
