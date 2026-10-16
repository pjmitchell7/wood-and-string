// Explicit tone choices. These never change note timing or play audio.
const DEFAULTS=Object.freeze({mode:'bypass',gain_db:0,low_cut_hz:90,high_cut_hz:5500,ambience:'off',ambience_mix:0,overload:'refuse'});
const CLEAN=Object.freeze({...DEFAULTS,mode:'clean',gain_db:-6});
const PRESETS=Object.freeze({clean:CLEAN,room:Object.freeze({...CLEAN,ambience:'room',ambience_mix:.1}),delay:Object.freeze({...CLEAN,ambience:'delay',ambience_mix:.1})});
export const ELECTRIC_TONE_CHOICES=Object.freeze([['dry','Dry guitar'],['clean','Clean'],['room','Clean + room'],['delay','Clean + delay']].map(Object.freeze));

export function normalizeElectricTone(value){
  if(value===undefined)return undefined;
  if(!value||Object.prototype.toString.call(value)!=='[object Object]'||Object.keys(value).some(key=>!Object.hasOwn(DEFAULTS,key)))throw Error('Invalid saved Electric tone settings.');
  const tone={...DEFAULTS,...value};
  if(!['bypass','clean'].includes(tone.mode)||!['off','room','delay'].includes(tone.ambience)||tone.overload!=='refuse')throw Error('Unsupported Electric tone settings.');
  for(const [key,low,high]of [['gain_db',-24,6],['low_cut_hz',60,160],['high_cut_hz',3500,7500],['ambience_mix',0,.25]])
    if(typeof tone[key]!=='number'||!Number.isFinite(tone[key])||tone[key]<low||tone[key]>high)throw Error('Electric tone setting is outside its range.');
  if(tone.ambience==='off'&&tone.ambience_mix!==0)throw Error('Choose a room or delay before setting its amount.');
  if(tone.mode==='bypass'){
    if(Object.keys(DEFAULTS).some(key=>tone[key]!==DEFAULTS[key]))throw Error('Dry guitar cannot silently discard tone settings.');
    return undefined;
  }
  return tone;
}
export function electricTonePreset(value){
  const tone=normalizeElectricTone(value);
  if(!tone)return 'dry';
  return Object.keys(PRESETS).find(key=>JSON.stringify(PRESETS[key])===JSON.stringify(tone))||'custom';
}
export function electricToneForPreset(name,saved){
  if(name==='dry')return undefined;
  if(Object.hasOwn(PRESETS,name))return {...PRESETS[name]};
  if(name==='custom'){
    const tone=normalizeElectricTone(saved);
    if(tone)return tone;
  }
  throw Error('Choose an available Electric tone.');
}
export function electricToneLabel(value){
  const tone=normalizeElectricTone(value);
  return !tone?'dry Electric guitar':tone.ambience_mix>0?`Electric guitar with ${tone.ambience}`:'clean Electric guitar';
}
export function electricToneDescription(value){
  const tone=normalizeElectricTone(value),preset=electricTonePreset(tone);
  if(!tone)return 'Dry guitar samples, with no added tone processing.';
  if(preset==='clean')return 'Clean filtering with a 6 dB level reduction.';
  if(preset==='room')return 'Clean tone with a light room effect and a 6 dB level reduction.';
  if(preset==='delay')return 'Clean tone with two quiet repeats and a 6 dB level reduction.';
  return `Saved custom clean tone with ${tone.ambience_mix>0?`${tone.ambience} at ${tone.ambience_mix*100}%`:'no added ambience'} and ${tone.gain_db} dB gain. Its saved settings are retained exactly.`;
}
