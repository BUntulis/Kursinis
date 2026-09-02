module.exports = {
  content: [
    "./dashboard/templates/**/*.html",
    "./dashboard/static/dashboard/**/*.js"
  ],
  theme: {
    extend: {
      colors: {
        benchmark: {
          bg: "#070a12",
          panel: "#0d1320",
          accent: "#38bdf8"
        }
      }
    }
  },
  plugins: []
};
