// Local paper-card motion; design references are recorded in docs/design-references.md.
// This module only changes decorative CSS variables. It has no desktop bridge access.
function mountCardMotion(stage, card) {
  const finePointer = window.matchMedia('(hover: hover) and (pointer: fine)');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const neutral = {'--card-rx':'0deg','--card-ry':'0deg','--card-lift':'0px','--shadow-x':'0px','--shadow-y':'4px','--sheen-opacity':'0'};
  let frame = null, pending = null, quietUntil = 0;
  const setStyle = values => {
    for (const [key, value] of Object.entries(values)) card.style.setProperty(key, value);
  };
  const reset = () => {
    if (frame !== null) window.cancelAnimationFrame(frame);
    frame = null; pending = null;
    setStyle(neutral);
  };
  const paused = () => !finePointer.matches || reducedMotion.matches || stage.hidden || card.hidden ||
    document.hidden || performance.now() < quietUntil || document.querySelector('dialog[open]') ||
    document.activeElement?.matches('input, textarea, [contenteditable="true"]');
  const move = event => {
    // Leave edit fields, buttons, touch input and held-pointer drags completely steady.
    if (paused() || event.buttons || event.pointerType !== 'mouse' ||
      event.target.closest('button, input, textarea, a, [contenteditable="true"]')) {
      reset(); return;
    }
    // Use the untransformed stage, avoiding feedback from the moving card's rectangle.
    const bounds = stage.getBoundingClientRect();
    if (!bounds.width || !bounds.height) { reset(); return; }
    const x = Math.max(-1, Math.min(1, 2 * (event.clientX - bounds.left) / bounds.width - 1));
    const y = Math.max(-1, Math.min(1, 2 * (event.clientY - bounds.top) / bounds.height - 1));
    pending = {
      '--card-rx':`${(-y * 2).toFixed(3)}deg`, '--card-ry':`${(x * 2.6).toFixed(3)}deg`,
      '--card-lift':'-0.8px', '--shadow-x':`${(-x * 1.6).toFixed(3)}px`,
      '--shadow-y':`${(4.8 - y * 0.8).toFixed(3)}px`,
      '--sheen-x':`${((x + 1) * 50).toFixed(2)}%`, '--sheen-y':`${((y + 1) * 50).toFixed(2)}%`,
      '--sheen-opacity':'1',
    };
    if (frame === null) frame = window.requestAnimationFrame(() => {
      frame = null;
      if (!paused() && pending) setStyle(pending);
      else reset();
      pending = null;
    });
  };
  const pause = () => { quietUntil = performance.now() + 240; reset(); };
  stage.addEventListener('pointermove', move, {passive:true});
  stage.addEventListener('pointerleave', reset);
  stage.addEventListener('pointerdown', reset);
  stage.addEventListener('pointercancel', reset);
  stage.addEventListener('focusin', reset);
  stage.addEventListener('wheel', pause, {passive:true});
  stage.addEventListener('scroll', pause, {capture:true,passive:true});
  window.addEventListener('blur', reset);
  window.addEventListener('resize', reset);
  document.addEventListener('visibilitychange', reset);
  finePointer.addEventListener('change', reset);
  reducedMotion.addEventListener('change', reset);
  new MutationObserver(() => { if (stage.hidden || card.hidden) reset(); }).observe(stage, {attributes:true,attributeFilter:['hidden']});
}

const motionStage = document.getElementById('card-stage');
const motionCard = document.getElementById('app');
if (motionStage && motionCard) mountCardMotion(motionStage, motionCard);
