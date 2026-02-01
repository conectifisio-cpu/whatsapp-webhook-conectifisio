export default async function handler(req, res) {
  // ===== 1) Verificação do webhook (GET) =====
  if (req.method === "GET") {
    const mode = req.query["hub.mode"];
    const token = req.query["hub.verify_token"];
    const challenge = req.query["hub.challenge"];

    if (mode === "subscribe" && token === process.env.VERIFY_TOKEN) {
      return res.status(200).send(challenge);
    }
    return res.status(403).send("Forbidden");
  }

  // ===== 2) Recebimento de eventos (POST) =====
  if (req.method === "POST") {
    try {
      // Vercel às vezes entrega body como string
      const body =
        typeof req.body === "string" ? JSON.parse(req.body) : req.body;

      console.log("[WEBHOOK][POST] received");

      // Percorre entries/changes (pode vir mais de um)
      const entries = body?.entry || [];
      for (const entry of entries) {
        const changes = entry?.changes || [];
        for (const change of changes) {
          const value = change?.value;
          if (!value) continue;

          // (Opcional) se quiser, você pode validar que o evento é do seu phone_number_id
          const incomingPhoneNumberId = value?.metadata?.phone_number_id;
          const myPhoneNumberId = process.env.PHONE_NUMBER_ID;

          // Se vier um payload de teste com phone_number_id fake, isso evita confusão.
          // MAS NÃO impede o teste do painel (ele só precisa de 200 OK).
          if (incomingPhoneNumberId && myPhoneNumberId && incomingPhoneNumberId !== myPhoneNumberId) {
            console.log(
              `[WEBHOOK] ignoring event for phone_number_id=${incomingPhoneNumberId} (expected ${myPhoneNumberId})`
            );
            continue;
          }

          const messages = value?.messages || [];
          for (const msg of messages) {
            const from = msg?.from; // ex: "5511999999999"
            const type = msg?.type;

            if (!from) continue;

            // Só vamos responder texto por enquanto
            const text = type === "text" ? (msg?.text?.body || "").trim() : "";

            console.log(`[WEBHOOK] incoming message type=${type} from=${from} text="${text}"`);

            if (!text) {
              // Se não for texto, só não responde (por enquanto)
              continue;
            }

            await sendText(from, `Recebi: ${text}`);
          }
        }
      }

      return res.status(200).send("OK");
    } catch (e) {
      console.error("Webhook error:", e);
      // Meta recomenda responder 200 mesmo em erro para evitar re-tentativas infinitas
      return res.status(200).send("OK");
    }
  }

  return res.status(405).send("Method Not Allowed");
}

async function sendText(to, body) {
  const phoneNumberId = process.env.PHONE_NUMBER_ID;
  const token = process.env.WHATSAPP_TOKEN;

  if (!phoneNumberId) {
    console.error("sendText: PHONE_NUMBER_ID missing");
    return;
  }
  if (!token) {
    console.error("sendText: WHATSAPP_TOKEN missing");
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
    console.log("SendText OK ->", to);
  }
}
