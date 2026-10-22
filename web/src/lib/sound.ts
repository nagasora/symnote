export function playClearSound(enabled: boolean) {
  if (!enabled || !("AudioContext" in window)) return;
  const context = new AudioContext();
  const oscillator = context.createOscillator();
  const gain = context.createGain();
  oscillator.frequency.setValueAtTime(660, context.currentTime);
  oscillator.frequency.exponentialRampToValueAtTime(
    880,
    context.currentTime + 0.11,
  );
  gain.gain.setValueAtTime(0.05, context.currentTime);
  gain.gain.exponentialRampToValueAtTime(0.001, context.currentTime + 0.18);
  oscillator.connect(gain).connect(context.destination);
  oscillator.start();
  oscillator.stop(context.currentTime + 0.18);
}
