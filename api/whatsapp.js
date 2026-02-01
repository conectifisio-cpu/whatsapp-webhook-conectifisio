export default async function handler(req, res) {
  // ===== 1) VERIFICAÇÃO DO WEBHOOK (GET) =====
  if (req.method === "GET") {
    const mode = req.query["hub.mode"];
    const token = req.query["hub.verify_token"];
    const challenge = req.query["hub.challenge"];

    console.log("[WEBHOOK] GET /api/whatsapp", {
      mode,
      tokenReceived: token,
      hasChallenge: Boolean(challenge),
    });

    if (mode === "subscribe" && token === process.env.VERIFY_TOKEN) {
      // Meta espera exatamente o challenge puro
      return res.status(200).send(challenge);
    }

    return res.status(403).send("Forbidden");
  }

  // ===== 2) RECEBIMENTO DE EVENTOS (POST) =====
  if (req.method === "POST") {
    try {
      console.log("[WEBHOOK] POST /api/whatsapp");
      console.log("[POST] keys:", Object.keys(req.body || {}));

      const entry = req.body?.entry?.[0];
      const change = entry?.changes?.[0];
      const value = change?.value;

      if (!value) {
        return res.status(200).send("OK");
      }

      // 2.1) Ignora status (delivered/read/etc.)
      if (value.statuses?.length) {
        console.log("[STATUS] ignoring statuses event");
        return res.status(200).send("OK");
      }

      // 2.2) Captura mensagem
      const msg = value.messages?.[0];
      if (!msg) {
        return res.status(200).send("OK");
      }

      const from = msg.from; // ex: "5511971904516"
      const type = msg.type;

      // Log útil
      console.log("[MESSAGE] incoming", {
        from,
        type,
        phone_number_id_in_payload: value?.metadata?.phone_number_id,
      });

      // Só responde texto simples
      if (type !== "text") {
        console.log("[MESSAGE] non-text received, ignoring");
        return res.status(200).send("OK");
      }

      const text = msg.text?.body?.trim() || "";
      console.log("[MESSAGE] text:", text);

      // Resposta (eco)
      await sendText(from, `Recebi: ${text}`);

      return res.status(200).send("OK");
    } catch (e) {
      console.error("[WEBHOOK] error:", e);
      // Importante: sempre 200 pro Meta não ficar re-tentando sem parar
      return res.status(200).send("OK");
    }
  }

  return res.status(405).send("Method Not Allowed");
}

async function sendText(to, body) {
  const phoneNumberId = process.env.PHONE_NUMBER_ID;
  const token = process.env.WHATSAPP_TOKEN;

  if (!phoneNumberId || !token) {
    console.error("[SEND] Missing env vars", {
      hasPhoneNumberId: Boolean(phoneNumberId),
      hasToken: Boolean(token),
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

  if (!resp.ok) {
    const err = await resp.text();
    console.error("[SEND] failed:", resp.status, err);
  } else {
    const data = await resp.json().catch(() => null);
    console.log("[SEND] ok", data);
  }
}
