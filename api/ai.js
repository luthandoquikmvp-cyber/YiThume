export default async function handler(req, res) {
  // CORS (optional - helpful if you call from other origins)
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");

  if (req.method === "OPTIONS") return res.status(200).end();
  if (req.method !== "POST") return res.status(405).json({ ok: false, error: "Method not allowed" });

  const key = process.env.OPENAI_API_KEY;
  if (!key) return res.status(500).json({ ok: false, error: "Missing OPENAI_API_KEY on server" });

  try {
    const { prompt, json } = req.body || {};
    if (!prompt || typeof prompt !== "string") {
      return res.status(400).json({ ok: false, error: "Missing prompt" });
    }

    const model = process.env.OPENAI_MODEL || "gpt-4.1-mini";

    // Call OpenAI Responses API (server-side)
    const r = await fetch("https://api.openai.com/v1/responses", {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${key}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        model,
        input: prompt,
        // If you want structured JSON back, you can enforce it:
        ...(json ? { text: { format: { type: "json_object" } } } : {})
      })
    });

    const data = await r.json();

    if (!r.ok) {
      return res.status(r.status).json({
        ok: false,
        error: data?.error?.message || "OpenAI request failed",
        raw: data
      });
    }

    // Extract plain text output safely
    const outText =
      data?.output_text ||
      (Array.isArray(data?.output) ? JSON.stringify(data.output) : "");

    return res.status(200).json({ ok: true, text: outText, raw: data });
  } catch (e) {
    return res.status(500).json({ ok: false, error: e?.message || String(e) });
  }
}