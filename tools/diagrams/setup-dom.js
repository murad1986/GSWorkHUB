// Excalidraw рассчитан на браузер. Этот модуль подкладывает ему DOM и обязан
// выполниться ДО импорта самого пакета — поэтому он вынесен отдельно и стоит
// первым в списке импортов рендера (порядок импортов в ESM гарантирован).
import { JSDOM } from 'jsdom';
import { createCanvas } from '@napi-rs/canvas';

const dom = new JSDOM('<!doctype html><html><body></body></html>', {
  pretendToBeVisual: true,
});
const g = globalThis;

g.window = dom.window;
g.document = dom.window.document;

// Excalidraw трогает эти глобалы прямо на загрузке модулей, поэтому переносим
// их из jsdom списком — иначе падает по одному на каждом импорте.
for (const name of [
  'Element', 'Node', 'HTMLElement', 'HTMLImageElement', 'HTMLCanvasElement',
  'HTMLDivElement', 'SVGElement', 'SVGSVGElement', 'Image', 'DOMParser',
  'XMLSerializer', 'Event', 'CustomEvent', 'KeyboardEvent', 'MouseEvent',
  'Blob', 'FileReader', 'MutationObserver', 'getComputedStyle',
  'requestAnimationFrame', 'cancelAnimationFrame',
]) {
  if (dom.window[name] !== undefined) g[name] = dom.window[name];
}

if (!g.ResizeObserver) {
  g.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
}

// FontFace API в jsdom нет вовсе. Заглушка нужна только чтобы пакет загрузился:
// сами шрифты в SVG подставляются как имена семейств, файлы не требуются.
if (!g.FontFace) {
  g.FontFace = class FontFace {
    constructor(family, source, descriptors = {}) {
      this.family = family;
      this.source = source;
      Object.assign(this, descriptors);
      this.status = 'loaded';
      this.loaded = Promise.resolve(this);
    }

    load() {
      return Promise.resolve(this);
    }
  };
}

const fonts = {
  _all: new Set(),
  add(f) { this._all.add(f); return this; },
  delete(f) { return this._all.delete(f); },
  forEach(cb) { this._all.forEach(cb); },
  load: () => Promise.resolve([]),
  check: () => true,
  ready: Promise.resolve(),
  addEventListener() {},
  removeEventListener() {},
};
if (!dom.window.document.fonts) {
  Object.defineProperty(dom.window.document, 'fonts', { value: fonts, configurable: true });
}
g.FontFaceSet = function FontFaceSet() {};
g.self = dom.window;
// browser-fs-access внутри Excalidraw проверяет `"top" in self && self !== top`,
// и без этих трёх глобалов падает ещё на загрузке модуля.
g.top = dom.window;
g.parent = dom.window;
g.location = dom.window.location;
g.devicePixelRatio = 1;

// navigator в Node доступен только для чтения — подменяем через defineProperty.
Object.defineProperty(g, 'navigator', {
  value: dom.window.navigator,
  configurable: true,
  writable: true,
});

if (!g.matchMedia) {
  g.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {} });
}

// jsdom не умеет getContext('2d') и возвращает null — Excalidraw на этом падает,
// потому что меряет ширину текста через canvas. Отдаём настоящий контекст из
// @napi-rs/canvas: метрики получаются такие же, как в браузере.
dom.window.HTMLCanvasElement.prototype.getContext = function getContext(kind) {
  if (kind !== '2d') return null;
  if (!this.__ctx) {
    const surface = createCanvas(this.width || 1, this.height || 1);
    this.__ctx = surface.getContext('2d');
  }
  return this.__ctx;
};

export { dom };
