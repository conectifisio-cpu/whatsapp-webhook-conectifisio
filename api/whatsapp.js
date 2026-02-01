export default async function handler(req, res) {
  // ====== 1) Verificação do webhook (GET) ======
  if (req.method === "GET") {
    const mode = req.query["hub.mode"];
    const token = req.query["hub.verify_token"];
    const challenge = req.query["hub.challenge"];

    const ok = mode === "subscribe" && token === process.env.VERIFY_TOKEN;

    if (ok) {
      console.log("[WEBHOOK][GET] verified OK");
      return res.status(200).send(challenge);
    }

    console.warn("[WEBHOOK][GET] forbidden", { mode, tokenReceived: token ? "***" : null });
    return res.status(403).send("Forbidden");
  }

  // ====== 2) Recebimento de eventos (POST) ======
  if (req.method === "POST") {
    try {
      // Log leve (evita imprimir token e coisas enormes)
      console.log("[WEBHOOK][POST] received");

      const entry = req.body?.entry?.[0];
      const change = entry?.changes?.[0];
      const value = change?.value;

      // Às vezes vem status update, não é mensagem de usuário
      const status = value?.statuses?.[0];
      if (status) {
        console.log("[WEBHOOK][POST] status update:", {
          id: status.id,
          status: status.status,
          timestamp: status.timestamp
        });
        return res.status(200).send("OK");
      }

      const msg = value?.messages?.[0];
      if (!msg) {
        // nada pra processar
        return res.status(200).send("OK");
      }

      const from = msg.from; // wa_id do usuário (número sem '+')
      const type = msg.type;

      let text = "";
      if (type === "text") {
        text = msg.text?.body?.trim() || "";
      } else {
        text = ""; // outros tipos (imagem, áudio etc)
      }

      console.log("[WEBHOOK][MSG] from:", from, "type:", type, "text:", text);

      // Resposta simples (eco)
      const reply = type === "text"
        ? `Recebi: ${text}`
        : `Recebi uma mensagem do tipo "${type}". (por enquanto eu respondo só texto)`;

      const sendResult = await sendText(from, reply);

      if (!sendResult.ok) {
        console.error("[SEND] failed:", sendResult.status, sendResult.bodyText);
      } else {
        console.log("[SEND] ok:", sendResult.status);
      }

      return res.status(200).send("OK");
    } catch (e) {
      console.error("[WEBHOOK] error:", e);
      // sempre responda 200 pro Meta não ficar reenviando em loop
      return res.status(200).send("OK");
    }
  }

  return res.status(405).send("Method Not Allowed");
}

async function sendText(to, body) {
  const phoneNumberId = process.env.PHONE_NUMBER_ID;
  const token = process.env.WHATSAPP_TOKEN;

  if (!phoneNumberId || !token) {
    const msg = "Missing env vars: PHONE_NUMBER_ID and/or WHATSAPP_TOKEN";
    console.error("[SEND] " + msg);
    return { ok: false, status: 500, bodyText: msg };
  }

  // Pode manter v19.0 se quiser; se preferir, troque para v24.0
  const url = `https://graph.facebook.com/v19.0/${phoneNumberId}/messages`;

  const payload = {
    messaging_product: "whatsapp",
    to,
    type: "text",
    text: { body }
  };

  const resp = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      Accept: "application/json"
    },
    body: JSON.stringify(payload)
  });

  const bodyText = await resp.text();
  return { ok: resp.ok, status: resp.status, bodyText };
}
