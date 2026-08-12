import React from "react";
import { createRoot } from "react-dom/client";
import MarkdownContent from "../src/components/MarkdownContent";

const source = "前文 $x^2$ 后文\n\n$$\\frac{a}{b}$$";

createRoot(document.getElementById("root")!).render(<MarkdownContent text={source} />);
