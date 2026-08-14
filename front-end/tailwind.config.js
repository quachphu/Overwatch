/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        // design_prompt.md: global body is Geist; Silkscreen is used for the stat number only.
        geist: ["Geist", "-apple-system", "BlinkMacSystemFont", "sans-serif"],
        silkscreen: ["Silkscreen", "cursive"],
      },
    },
  },
  plugins: [],
};
