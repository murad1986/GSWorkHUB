// Локальный рендер: .excalidraw → SVG → PNG настоящим движком Excalidraw.
//
// Нужен, чтобы смотреть на схему глазами, а не считать её готовой по коду.
// Ничего не отправляется наружу: и рендер, и просмотр на своей машине.
//
// Пакет Excalidraw рассчитан на сборщик, голым Node не запускается, поэтому
// файл сначала собирается esbuild-ом:
//
//   npm run render            — собрать и отрисовать out/sipoc.excalidraw
//   node render.build.mjs <вход.excalidraw> <выход.png> [ширина]

import './setup-dom.js';
import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { exportToSvg } from '@excalidraw/excalidraw';

const require = createRequire(import.meta.url);
const { Resvg } = require('@resvg/resvg-js');

const [, , inPath, outPath, widthArg] = process.argv;
if (!inPath || !outPath) {
  console.error('нужно: node render.build.mjs <вход.excalidraw> <выход.png> [ширина]');
  process.exit(1);
}

const scene = JSON.parse(readFileSync(inPath, 'utf-8'));

const svgNode = await exportToSvg({
  elements: scene.elements,
  appState: { ...scene.appState, exportBackground: true, exportWithDarkMode: false },
  files: scene.files || {},
});

// Excalidraw пишет xmlns атрибутом, и XMLSerializer добавляет свой же — в
// корневом теге он оказывается дважды. Такой файл невалиден как XML, и
// разборщики его не берут: Figma отвечает «Unable to convert SVG», а
// rsvg-convert падает на «Attribute xmlns redefined». Проверено 3 августа 2026
// на схеме рабочего контура: браузеры файл рисуют, поэтому глазами
// поломка не видна вовсе.
function dedupeXmlns(text) {
  const end = text.indexOf('>');
  if (end === -1) return text;
  let seen = false;
  const head = text.slice(0, end).replace(/\sxmlns="[^"]*"/g, (found) => {
    if (seen) return '';
    seen = true;
    return found;
  });
  return head + text.slice(end);
}

const svg = dedupeXmlns(new globalThis.XMLSerializer().serializeToString(svgNode));
const svgPath = outPath.replace(/\.png$/, '.svg');
writeFileSync(svgPath, svg, 'utf-8');

const resvg = new Resvg(svg, {
  background: scene.appState?.viewBackgroundColor || '#ffffff',
  fitTo: { mode: 'width', value: Number(widthArg) || 2000 },
});
writeFileSync(outPath, resvg.render().asPng());

console.log(`${scene.elements.length} элементов → ${svgPath}, ${outPath}`);
