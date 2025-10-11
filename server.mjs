// server.mjs
import express from "express";
import { promises as fs } from "fs";
import path from "path";

const app = express();
const ROOT = process.cwd();
const RUNS_DIR = path.join(ROOT, "runs");

app.use(express.static(ROOT)); // sert index.html + runs/*

app.get("/api/sources", async (req, res) => {
  try {
    const entries = await fs.readdir(RUNS_DIR, { withFileTypes: true });
    const sources = [];
    for (const ent of entries) {
      if (!ent.isDirectory()) continue;
      const label = ent.name;
      const csv = path.join("runs", label, "scored.csv");
      try {
        await fs.stat(path.join(ROOT, csv));
        sources.push({ label, path: csv });
      } catch {}
    }
    sources.sort((a, b) => a.label.localeCompare(b.label));
    res.json({ sources });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

app.listen(8080, () => console.log("http://localhost:8080"));
