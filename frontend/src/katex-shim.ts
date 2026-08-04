export default {
  renderToString(): string {
    throw new Error("The web renderer uses MathJax instead of KaTeX.");
  },
};
