"""Pure calibration-contract tests. No DLL, samples, audio or inference."""
import copy
import math
from pathlib import Path
import re
import unittest

from audio import dynamics as d


BASE = (Path(__file__).resolve().parents[1]/'audio/provenance/meta-steel-guitar.sfz').read_text()


def manifest():
    entries = []
    for region in d.regions(BASE):
        ref = .8; unit = 1.
        entries.append({**region, 'sample_sha256': 'a'*64, 'unit_rms': unit,
            'reference_rms': ref, 'gain_db': d.region_gain_db(unit, ref), 'status': 'accepted',
            'validation': [{'velocity': v, 'measurement': {'rms': {'250ms': ref*10**(d.velocity_db(v)/20)}}}
                           for v in d.VALIDATION_VELOCITIES[region['layer']]]})
    return {'schema': d.SCHEMA, 'policy': d.POLICY, 'status': 'accepted-reference-only',
        'source_sfz_text_sha256': d.text_hash(BASE), 'reference_window_ms': 250,
        'gain_limit_db': 12., 'regions': entries, 'excluded_remaps': d.excluded_remaps(BASE)}


class DynamicsTests(unittest.TestCase):
    def test_actual_inventory_preserves_missing_low_e_pitch(self):
        rows = d.regions(BASE)
        self.assertEqual(len(rows), 375)
        self.assertEqual(len({r['sample'] for r in rows}), 375)
        self.assertFalse(any(r['string'] == 0 and r['pitch'] == 47 for r in rows))
        self.assertEqual(len([r for r in rows if r['layer'] == 2]), 125)
        self.assertEqual(len(d.excluded_remaps(BASE)), 3)
        with self.assertRaises(ValueError): d.regions(BASE.replace('<master> lochan=2 hichan=2', '<master> lochan=2 hichan=3'))
        with self.assertRaises(ValueError): d.regions(BASE.replace('amp_velcurve_48=1', 'volume=4'))

    def test_curve_range_anchor_and_declared_span(self):
        curve = d.velocity_curve()
        self.assertEqual(set(curve), set(range(1, 128)))
        self.assertTrue(all(0 < x <= 1 for x in curve.values()))
        self.assertTrue(all(curve[v] < curve[v+1] for v in range(1,127)))
        self.assertEqual(d.velocity_db(80), 0)
        self.assertAlmostEqual(d.velocity_db(127)-d.velocity_db(1), 18)
        for value in (True, 0, 128, 80., math.nan):
            with self.assertRaises(ValueError): d.velocity_db(value)

    def test_compensation_and_curve_recover_target_at_every_velocity(self):
        unit=.012; ref=.008
        gain=d.region_gain_db(unit,ref);curve=d.velocity_curve()
        for velocity in range(1,128):
            observed=unit*10**(gain/20)*curve[velocity]
            self.assertAlmostEqual(20*math.log10(observed/ref),d.velocity_db(velocity),places=12)
        for pair in ((0.,1.),(1.,0.),(1e-10,1.),(math.nan,1.),(1.,math.inf),(True,1.),(.0001,1.),(1.,.0001)):
            with self.assertRaises(ValueError): d.region_gain_db(*pair)

    def test_transform_keeps_samples_ranges_choking_and_source_immutable(self):
        m=manifest(); before=copy.deepcopy(m)
        for string in range(6):
            mapped=d.calibrated_string_sfz(BASE,string,m)
            expected=[r for r in d.regions(BASE) if r['string']==string]
            self.assertEqual(re.findall(r'sample=(\S+)',mapped),[r['sample'] for r in expected])
            self.assertEqual([int(x) for x in re.findall(r'<region> key=(\d+)',mapped)],[r['pitch'] for r in expected])
            self.assertEqual(mapped.count(f'group={string+1} off_by={string+1}'),len(expected))
            self.assertIn('ampeg_release=0.05 pitch_random=0 amp_random=0 off_mode=normal',mapped)
            self.assertEqual(mapped.count('amp_velcurve_80='),len(expected))
            self.assertNotIn('lochan=',mapped)
            self.assertNotIn('pitch_keycenter=',mapped)
        self.assertEqual(m,before)

    def test_partial_unverified_changed_and_tampered_manifests_rejected(self):
        good=manifest()
        for fault in ('status','missing','duplicate','map','gain','sample','validation','bound',
                      'null','unhashable','hash-type','validation-type','validation-element','rms-bool'):
            m=copy.deepcopy(good)
            if fault=='status':m['status']='rejected'
            if fault=='missing':m['regions'].pop()
            if fault=='duplicate':m['regions'][1]=m['regions'][0]
            if fault=='map':m['source_sfz_text_sha256']='0'*64
            if fault=='gain':m['regions'][0]['gain_db']+=.2
            if fault=='sample':m['regions'][0]['sample']='different.ogg'
            if fault=='validation':m['regions'][0]['validation'][0]['measurement']['rms']['250ms']*=1.1
            if fault=='bound':m['regions'][0]['unit_rms']=1e-6
            if fault=='null':m=None
            if fault=='unhashable':m['regions'][0]['id']=[]
            if fault=='hash-type':m['regions'][0]['sample_sha256']=4
            if fault=='validation-type':m['regions'][0]['validation']=None
            if fault=='validation-element':m['regions'][0]['validation']=[None]
            if fault=='rms-bool':m['regions'][0]['validation'][0]['measurement']['rms']['250ms']=True
            with self.subTest(fault=fault),self.assertRaises(ValueError):d.calibrated_string_sfz(BASE,0,m)
        for string in (True,-1,6,'0'):
            with self.assertRaises(ValueError):d.calibrated_string_sfz(BASE,string,good)

    def test_individually_valid_levels_cannot_hide_boundary_inversion(self):
        m = manifest()
        pair = (m['regions'][0]['string'], m['regions'][0]['pitch'])
        for entry in m['regions']:
            if (entry['string'], entry['pitch']) != pair:
                continue
            for validation in entry['validation']:
                delta = {48: .099, 49: -.099}.get(validation['velocity'], 0)
                validation['measurement']['rms']['250ms'] *= 10**(delta/20)
        # Both errors are below0.1dB, but48->49 reverses by about0.055dB.
        with self.assertRaisesRegex(ValueError, 'boundary is inverted'):
            d.calibrated_string_sfz(BASE, 0, m)


if __name__=='__main__':unittest.main()
