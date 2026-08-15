import { useState } from "react";
import Hero from "./Hero";

/**
 * The landing page is the hero and nothing else — design_prompt.md is explicit that there is
 * no routing and no second section.
 *
 * `Hero` is the spec's markup verbatim; this file only supplies Overwatch's copy and wires the
 * CTA to the real ingress. Rendering `<Hero />` with no props gives back the reference hero
 * exactly as written in the spec.
 */

type Status =
  | { kind: "idle" }
  | { kind: "working" }
  | { kind: "error"; message: string };

export default function App() {
  const [status, setStatus] = useState<Status>({ kind: "idle" });

  const start = async (url: string) => {
    if (!url.trim()) {
      setStatus({ kind: "error", message: "Paste the URL you want scanned." });
      return;
    }
    setStatus({ kind: "working" });

    try {
      const response = await fetch("/api/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      const data = await response.json().catch(() => ({}));

      if (!response.ok) {
        setStatus({
          kind: "error",
          message: String(data.detail ?? "Could not start the scan."),
        });
        return;
      }
      window.location.href = `/report/${data.scan_id}`;
    } catch (error) {
      setStatus({
        kind: "error",
        message: error instanceof Error ? error.message : "Could not reach the server.",
      });
    }
  };

  const note =
    status.kind === "working"
      ? "Starting your scan…"
      : status.kind === "error"
        ? status.message
        : undefined;

  // No invented customer counts and no invented testimonial. Both cards describe how the
  // system actually works, which is checkable in the report, instead of asserting traction we
  // do not have. CLAUDE.md rule 7 applies to the landing page too.
  return (
    <Hero
      wordmark="overwatch"
      headline="QA that hires humans to check its own work"
      statNumber="2"
      statBody="Independent human panels per scan: one verifies the findings, one judges whether the report got better."
      testimonialBrand="Method"
      testimonialBrandInitial="M"
      quote="Agents scan your app and rank what they find. We then pay real people to say which findings are real, and re-rank on their answers."
      personName="Verified by Terac"
      personRole="Round 2 panel never sees round 1"
      avatarSrc=""
      emailPlaceholder="Paste your app URL"
      inputType="url"
      ctaLabel={status.kind === "working" ? "Working…" : "Scan my app"}
      onSubmit={start}
      note={note}
    />
  );
}
