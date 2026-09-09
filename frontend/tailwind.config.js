/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "sans-serif",
        ],
      },
      colors: {
        // Brand accent - a deep indigo/violet, used deliberately and only
        // for primary actions/focus/links/the logo mark. Everything else
        // stays on the slate neutral scale.
        //
        // Deliberately IDENTICAL to the sibling private-document-assistant
        // frontend's scale: the two products are a matched pair, and share
        // a visual language. What differs is the logo mark (a database
        // cylinder rather than a padlock) and every word of copy.
        brand: {
          50: "#f2f1fe",
          100: "#e6e4fd",
          200: "#cfccfb",
          300: "#aca5f7",
          400: "#8b7ef2",
          500: "#7161ec",
          600: "#5f45e0",
          700: "#5136c4",
          800: "#432e9e",
          900: "#392a7d",
          950: "#241c54",
        },
      },
      boxShadow: {
        card: "0 1px 2px 0 rgb(0 0 0 / 0.04), 0 1px 3px 0 rgb(0 0 0 / 0.06)",
      },
    },
  },
  plugins: [],
};
