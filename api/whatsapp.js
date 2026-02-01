// ===== Memória simples (funciona bem em teste; em produção a gente troca por banco/KV) =====
const processedMessageIds = new Set();
const lastChoiceByUser = new Map(); // { waId: "menu" | "humano" | ... }

export default async function handler(req, res) {
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
      const entry = req.body?.entry?.[0];
      const change = entry?.changes?.[0];
      const value = change?.value;

      // Ignora evento vazio
      if (!value) return res.status(200).send("OK");

      // Ignora status (delivered/read/etc.)
      if (value.statuses?.length) return res.status(200).send("OK");

      const msg = value.messages?.[0];
      if (!msg) return res.status(200).send("OK");

      const from = msg.from;        // ex: "5511971904516"
      const msgId = msg.id;         // id único
      const type = msg.type;

      // Dedupe básico (Meta pode reenviar)
      if (msgId && processedMessageIds.has(msgId)) {
        return res.status(200).send("OK");
      }
      if (msgId) processedMessageIds.add(msgId);

      // Só texto por enquanto
      if (type !== "text") {
        await sendText(from, "Por enquanto eu entendo apenas mensagens de texto 🙂\nDigite 0 para ver o menu.");
        return res.status(200).send("OK");
      }

      const text = (msg.text?.body || "").trim();

      // Normaliza
      const t = text.toLowerCase();

      // Comandos rápidos
      if (t === "0" || t === "menu") {
        lastChoiceByUser.set(from, "menu");
        await sendText(from, getMenu());
        return res.status(200).send("OK");
      }

      if (t === "9" || t.includes("atendente") || t.includes("humano")) {
        lastChoiceByUser.set(from, "humano");
        await sendText(
          from,
          "Certo. Me diga seu *nome* e *o que você precisa* que eu já encaminho pra equipe.\n\n(Para voltar ao menu: digite 0)"
        );
        return res.status(200).send("OK");
      }

      // Se usuário está no modo "humano", só confirma recebimento
      if (lastChoiceByUser.get(from) === "humano") {
        // Aqui depois a gente integra com WhatsApp da recepção / e-mail / planilha / CRM
        await sendText(from, "Perfeito — recebi. Já vou encaminhar pra equipe. ✅\n\n(Para voltar ao menu: digite 0)");
        return res.status(200).send("OK");
      }

      // Opções do menu
      if (t === "1") {
        await sendText(
          from,
          "🗓️ *Agendamento*\nMe diga:\n1) Nome completo\n2) Queixa / objetivo\n3) Melhor dia/horário\n\n(Para falar com atendente: 9 | Menu: 0)"
        );
        return res.status(200).send("OK");
      }

      if (t === "2") {
        await sendText(
          from,
          "💰 *Valores*\n• Sessão avulsa: sob consulta\n• RPG: R$ 150,00\n\nSe quiser, me diga qual serviço você procura. (Menu: 0)"
        );
        return res.status(200).send("OK");
      }

      if (t === "3") {
        await sendText(
          from,
          "📍 *Endereço*\nRua Alegre, 667 — Santa Paula — São Caetano do Sul/SP.\n\nQuer a rota no Google Maps? Responda: *SIM*.\n(Menu: 0)"
        );
        return res.status(200).send("OK");
      }

      if (t === "sim" && lastChoiceByUser.get(from) !== "humano") {
        await sendText(
          from,
          "Aqui está a rota (copie e cole no Maps):\nhttps://www.google.com/maps/search/?api=1&query=Rua+Alegre+667+Sao+Caetano+do+Sul"
        );
        return res.status(200).send("OK");
      }

      // Se chegou aqui, não entendeu: manda menu
      await sendText(from, "Não entendi. Digite *0* para ver o menu.");
      return res.status(200).send("OK");
    } catch (e) {
      console.error("Webhook error:", e);
      return res.status(200).send("OK");
    }
  }

  return res.status(405).send("Method Not Allowed");
}

function getMenu() {
  return (
    "👋 Olá! Como posso ajudar?\n\n" +
    "1) Agendar\n" +
    "2) Valores\n" +
    "3) Endereço\n" +
    "9) Falar com atendente\n\n" +
    "Responda com o número da opção."
  );
}

async function sendText(to, body) {
  const phoneNumberId = process.env.PHONE_NUMBER_ID;
  const token = process.env.WHATSAPP_TOKEN;

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
  }
}
