import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          950: "#0b0f14",
          900: "#111721",
          800: "#1a2231",
          700: "#263246",
        },
      },
      fontFamily: {
        mono: ["var(--font-mono, ui-monospace)", "monospace"],
      },
    },
  },
  plugins: [],
};
export default config;
