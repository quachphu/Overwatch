import { useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { ChevronDown, Menu, X } from "lucide-react";

/**
 * Full-screen cinematic hero, built strictly to `front-end/design_prompt.md`.
 *
 * Everything structural is fixed by that spec and reproduced verbatim: the layout
 * architecture, every breakpoint, the responsive near-black -> white colour flips, the glass
 * opacities and blur radii, the CTA gradient, the exact logo path, the CloudFront video, the
 * Silkscreen stat number, and all nine items on its animation checklist.
 *
 * Only the *copy* is parameterised, and every prop defaults to the exact string in the spec —
 * so `<Hero />` with no props renders the reference hero character for character, while
 * `App.tsx` passes Overwatch's own copy through the identical markup.
 */

const VIDEO_SRC =
  "https://d8j0ntlcm91z4.cloudfront.net/user_38xzZboKViGWJOttwIXH07lWA1P/hf_20260803_192301_9231ed6b-c55c-4a48-909c-4ebe11cf2e11.mp4";

const LOGO_PATH =
  "M 128 128 C 128 198.692 70.692 256 0 256 C 0 185.308 57.308 128 128 128 Z M 128 128 C 198.692 128 256 185.308 256 256 C 185.308 256 128 198.692 128 128 Z M 0 0 C 70.692 0 128 57.308 128 128 C 57.308 128 0 70.692 0 0 Z M 256 0 C 256 70.692 198.692 128 128 128 C 128 57.308 185.308 0 256 0 Z";

/** design_prompt.md: `linear-gradient(to bottom, #2b2b2b, #101010)` on every primary action. */
const CTA_GRADIENT = "linear-gradient(to bottom, #2b2b2b, #101010)";

const NAV_LINKS = [
  { label: "Modules", hasChevron: false },
  { label: "Clientele", hasChevron: false },
  { label: "Solutions", hasChevron: true },
  { label: "Billing", hasChevron: false },
] as const;

export interface HeroProps {
  wordmark?: string;
  headline?: string;
  statNumber?: string;
  statBody?: string;
  testimonialBrand?: string;
  testimonialBrandInitial?: string;
  quote?: string;
  personName?: string;
  personRole?: string;
  avatarSrc?: string;
  emailPlaceholder?: string;
  /** The spec's field is an email capture; Overwatch's ingress is a URL. */
  inputType?: "email" | "url";
  ctaLabel?: string;
  onSubmit?: (value: string) => void;
  /**
   * Optional status line under the form. Submitting starts a real scan, and an async action
   * with no acknowledgement reads as a dead button — but it renders nothing unless supplied,
   * so the reference hero keeps the spec's exact node tree.
   */
  note?: ReactNode;
}

export default function Hero({
  wordmark = "nexum",
  headline = "Ship AI workers that grind while you rest",
  statNumber = "42,500+",
  statBody = "Teams run Nexum to handle recurring ops daily.",
  testimonialBrand = "Stratify",
  testimonialBrandInitial = "S",
  quote = "With Nexum we went from managing tedious operational work to having AI agents that handle everything.",
  personName = "Sara Klein",
  personRole = "Dir of Operations",
  avatarSrc = "https://i.pravatar.cc/72?img=12",
  emailPlaceholder = "Type your email",
  inputType = "email",
  ctaLabel = "Get started",
  onSubmit,
  note,
}: HeroProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [value, setValue] = useState("");

  // design_prompt.md: "Opening menu locks document.body.style.overflow = 'hidden'".
  // Reset on unmount too, so a hot reload with the menu open cannot leave the page unscrollable.
  useEffect(() => {
    document.body.style.overflow = menuOpen ? "hidden" : "";
    return () => {
      document.body.style.overflow = "";
    };
  }, [menuOpen]);

  // Not in the spec's checklist, but a drawer that traps you with no keyboard exit is a
  // defect in any drawer. Escape only closes; it changes nothing else.
  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [menuOpen]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    onSubmit?.(value);
  };

  return (
    <section className="relative h-screen w-full overflow-hidden">
      {/* Full-bleed background video. No overlay gradient — content sits directly over it. */}
      <video
        className="absolute inset-0 h-full w-full object-cover"
        src={VIDEO_SRC}
        autoPlay
        loop
        muted
        playsInline
      />

      <div className="relative z-10 flex h-full flex-col">
        {/* ── Nav ──────────────────────────────────────────────────────────────────── */}
        <nav className="flex items-center justify-between px-5 py-5 sm:px-8 sm:py-6 lg:px-12">
          <a
            href="/"
            className="flex items-center gap-2 text-[#010101] lg:text-white"
            aria-label={wordmark}
          >
            <svg
              width="24"
              height="24"
              viewBox="0 0 256 256"
              className="fill-[#010101] lg:fill-white"
              aria-hidden="true"
            >
              <path d={LOGO_PATH} />
            </svg>
            <span className="text-lg font-semibold">{wordmark}</span>
          </a>

          {/* Desktop: glass pill cluster, then a separate gradient CTA pill. */}
          <div className="hidden items-center gap-3 md:flex">
            <div className="flex items-center gap-1 rounded-full bg-white/10 px-1.5 py-1.5 backdrop-blur-lg">
              {NAV_LINKS.map((link) => (
                <a
                  key={link.label}
                  href="#"
                  className="flex items-center gap-1 rounded-full px-4 py-1.5 text-sm font-medium text-white/80 transition-colors hover:bg-white/10 hover:text-white"
                >
                  {link.label}
                  {link.hasChevron && <ChevronDown className="h-3.5 w-3.5" />}
                </a>
              ))}
            </div>
            <a
              href="#start"
              className="flex items-center self-stretch rounded-full px-5 text-sm font-medium text-white transition-opacity hover:opacity-90"
              style={{ background: CTA_GRADIENT }}
            >
              {ctaLabel}
            </a>
          </div>

          {/* Mobile: circular glass button whose two icons morph into each other. */}
          <button
            type="button"
            onClick={() => setMenuOpen((open) => !open)}
            aria-label={menuOpen ? "Close menu" : "Open menu"}
            aria-expanded={menuOpen}
            className="relative z-50 flex h-10 w-10 items-center justify-center rounded-full bg-white/10 backdrop-blur-lg md:hidden"
          >
            <Menu
              className={`absolute h-5 w-5 text-[#010101] transition-all duration-300 lg:text-white ${
                menuOpen ? "rotate-90 scale-0 opacity-0" : "rotate-0 scale-100 opacity-100"
              }`}
            />
            <X
              className={`absolute h-5 w-5 text-[#010101] transition-all duration-300 lg:text-white ${
                menuOpen ? "rotate-0 scale-100 opacity-100" : "-rotate-90 scale-0 opacity-0"
              }`}
            />
          </button>
        </nav>

        {/* ── Mobile backdrop ──────────────────────────────────────────────────────── */}
        <div
          onClick={() => setMenuOpen(false)}
          aria-hidden={!menuOpen}
          className={`fixed inset-0 z-40 bg-black/80 backdrop-blur-md transition-opacity duration-300 md:hidden ${
            menuOpen ? "opacity-100" : "pointer-events-none opacity-0"
          }`}
        />

        {/* ── Mobile drawer ────────────────────────────────────────────────────────── */}
        <div
          className={`fixed right-0 top-0 z-40 flex h-full w-72 flex-col bg-black/90 backdrop-blur-xl transition-transform duration-500 ease-[cubic-bezier(0.16,1,0.3,1)] md:hidden ${
            menuOpen ? "translate-x-0" : "translate-x-full"
          }`}
        >
          <div className="flex flex-col gap-2 px-6 pt-24">
            {NAV_LINKS.map((link, index) => (
              <a
                key={link.label}
                href="#"
                onClick={() => setMenuOpen(false)}
                className="flex items-center justify-between rounded-xl px-4 py-3.5 text-base font-medium text-white/80 transition-all hover:bg-white/10 hover:text-white"
                style={{
                  opacity: menuOpen ? 1 : 0,
                  transform: menuOpen ? "translateX(0)" : "translateX(24px)",
                  transitionDelay: menuOpen ? `${(index + 1) * 60}ms` : "0ms",
                }}
              >
                {link.label}
                {link.hasChevron && <ChevronDown className="h-4 w-4" />}
              </a>
            ))}
          </div>

          <div className="mt-auto px-6 pb-10">
            <a
              href="#start"
              onClick={() => setMenuOpen(false)}
              className="block w-full rounded-full px-6 py-3 text-center text-sm font-medium text-white transition-all duration-[400ms] hover:opacity-90"
              style={{
                background: CTA_GRADIENT,
                opacity: menuOpen ? 1 : 0,
                transform: menuOpen ? "translateY(0)" : "translateY(16px)",
                transitionDelay: menuOpen ? "300ms" : "0ms",
              }}
            >
              {ctaLabel}
            </a>
          </div>
        </div>

        {/* ── Bottom-anchored content ──────────────────────────────────────────────── */}
        <div className="mt-auto flex flex-col gap-6 px-5 pb-8 sm:gap-8 sm:px-8 sm:pb-12 lg:flex-row lg:items-end lg:justify-between lg:px-12 lg:pb-16">
          {/* Left: headline + email capture */}
          <div className="max-w-xl">
            <h1 className="text-3xl font-semibold leading-[1.1] tracking-tight text-[#010101] sm:text-4xl lg:text-[3.5rem] lg:text-white">
              {headline}
            </h1>

            <form
              id="start"
              onSubmit={submit}
              className="mt-6 flex flex-col gap-3 sm:mt-8 sm:inline-flex sm:flex-row sm:items-center sm:gap-0 sm:rounded-full sm:bg-white sm:p-1.5"
            >
              <label htmlFor="hero-input" className="sr-only">
                {emailPlaceholder}
              </label>
              <input
                id="hero-input"
                type={inputType}
                value={value}
                onChange={(event) => setValue(event.target.value)}
                placeholder={emailPlaceholder}
                className="rounded-full bg-white px-5 py-3 text-sm text-gray-900 placeholder-gray-400 outline-none sm:w-64 sm:rounded-none sm:bg-transparent sm:px-4 sm:py-2"
              />
              <button
                type="submit"
                className="rounded-full px-6 py-3 text-sm font-medium text-white transition-opacity hover:opacity-90 sm:py-2.5"
                style={{ background: CTA_GRADIENT }}
              >
                {ctaLabel}
              </button>
            </form>

            {note && (
              <p className="mt-3 text-sm text-[#010101]/70 lg:text-white/70">{note}</p>
            )}
          </div>

          {/* Right: two glass cards */}
          <div className="flex w-full flex-col gap-4 sm:flex-row lg:w-auto lg:gap-5">
            {/* Stats card */}
            <div className="flex flex-col justify-between rounded-2xl bg-white/10 p-5 backdrop-blur-lg sm:w-64 sm:p-6">
              <p
                className="text-3xl font-normal tracking-tight text-[#010101] sm:text-4xl lg:text-white"
                style={{ fontFamily: "'Silkscreen', cursive" }}
              >
                {statNumber}
              </p>
              <p className="mt-3 text-sm leading-relaxed text-[#010101]/70 sm:mt-4 lg:text-white/70">
                {statBody}
              </p>
            </div>

            {/* Testimonial card */}
            <div className="rounded-2xl bg-white/10 p-5 backdrop-blur-lg sm:w-64 sm:p-6">
              <div className="mb-3 flex items-center gap-2 sm:mb-4">
                <div className="flex h-6 w-6 items-center justify-center rounded bg-black text-xs font-bold text-white">
                  {testimonialBrandInitial}
                </div>
                <span className="text-sm font-semibold text-[#010101] lg:text-white">
                  {testimonialBrand}
                </span>
              </div>

              <p className="text-sm leading-relaxed text-[#010101]/80 lg:text-white/80">
                &ldquo;{quote}&rdquo;
              </p>

              <div className="mt-4 flex items-center gap-3 sm:mt-5">
                {/* Falls back to the logo mark when no avatar is given, so a card that
                    credits a panel rather than a person does not need a stock face. */}
                {avatarSrc ? (
                  <img
                    src={avatarSrc}
                    alt={personName}
                    className="h-9 w-9 rounded-full bg-white/20 object-cover"
                  />
                ) : (
                  <div className="flex h-9 w-9 items-center justify-center rounded-full bg-white/20">
                    <svg
                      width="16"
                      height="16"
                      viewBox="0 0 256 256"
                      className="fill-[#010101] lg:fill-white"
                      aria-hidden="true"
                    >
                      <path d={LOGO_PATH} />
                    </svg>
                  </div>
                )}
                <div>
                  <p className="text-sm font-semibold text-[#010101] lg:text-white">
                    {personName}
                  </p>
                  <p className="text-xs text-[#010101]/60 lg:text-white/60">{personRole}</p>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
