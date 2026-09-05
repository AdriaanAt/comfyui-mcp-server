-- Correction: the solar dual-lens camera is NOT a Sonoff product.
--
-- The seed in 002 recorded an ONVIF/RTSP finding about Sonoff cameras. That
-- finding is accurate for Sonoff cameras and stays. What was wrong was the
-- inference that it applied to the solar dual-linkage camera actually owned:
-- that unit appeared alongside Sonoff devices on a shopping list and was
-- assumed to be one. It is an unbranded solar dual-lens unit with no Sonoff,
-- eWeLink or Zigbee affiliation, so nothing is known about its local access.
--
-- This migration narrows the Sonoff rule so it cannot be misread, and files
-- the real camera as 'unknown' with the steps that would settle it.
--
--   psql -d homestead -f homestead/sql/003_correct_camera_facts.sql

BEGIN;

-- Migrate any database seeded before 002 was corrected. On a fresh install
-- 002 already carries the scoped subject, so both statements match nothing
-- and are no-ops - which is what keeps the whole set re-runnable.
--
-- Order matters. A database seeded with the OLD 002 and then updated will
-- have the corrected row inserted by the new 002 while the stale row is still
-- present, so the stale one is dropped first; renaming it instead would leave
-- two rows with the same subject.
DELETE FROM compatibility_rules
WHERE subject = 'SONOFF cameras (ONVIF/RTSP)'
  AND EXISTS (
      SELECT 1 FROM compatibility_rules c
      WHERE c.subject = 'SONOFF-branded cameras (CAM-PT2 and similar)'
  );

UPDATE compatibility_rules
SET subject = 'SONOFF-branded cameras (CAM-PT2 and similar)',
    notes   = COALESCE(notes, '') ||
              ' SCOPE: applies only to Sonoff-branded cameras sold through '
              'eWeLink. Does NOT apply to unbranded solar or 4G cameras, '
              'which are a different market with different firmware.'
WHERE subject = 'SONOFF cameras (ONVIF/RTSP)';

-- Anti-join guard, for the same reason as 002: no unique constraint on
-- subject, so re-running must not duplicate these rows.
INSERT INTO compatibility_rules
    (subject, verdict, requires, caveat, evidence_url, checked_on, notes)
SELECT i.* FROM (VALUES

('Solar dual-linkage camera (unbranded, dual-lens PIR, WiFi/4G)',
 'unknown',
 'Identify the vendor app first - that names the ecosystem and decides '
 'whether any local path exists at all.',
 'Two independent obstacles, either of which alone rules out a conventional '
 'NVR setup. (1) Local access: unbranded solar cameras are usually locked to '
 'a vendor cloud app and frequently expose no RTSP or ONVIF at all; 4G '
 'variants are often pure cloud relay with no LAN presence whatsoever. '
 '(2) Battery duty cycle: a solar/battery PIR camera sleeps to conserve '
 'power and wakes on motion. Even where RTSP exists it will not hold a '
 'continuous stream, so continuous recording in Frigate or go2rtc is not '
 'achievable - event clips on motion are the realistic ceiling.',
 NULL,
 DATE '2026-09-04',
 'Deliberately unknown, not optimistic. An earlier assessment wrongly treated '
 'this as a Sonoff camera and inherited Sonoff''s ONVIF support; it is not a '
 'Sonoff device and that finding does not transfer. Do not plan an NVR or '
 'object-detection pipeline around this camera until local access is proven '
 'on the actual unit.'),

-- The general lesson, so the next unbranded purchase gets the same scrutiny.
('Solar or battery-powered PIR cameras (general)',
 'works_with_caveats',
 'Mains-powered ONVIF/RTSP cameras for anything needing a continuous stream.',
 'Battery and solar cameras sleep between PIR events by design. They cannot '
 'sustain a 24/7 stream regardless of protocol support, so they suit event '
 'alerting rather than continuous recording. Choosing one for an NVR role is '
 'a hardware mismatch that no software setting resolves.',
 NULL,
 DATE '2026-09-04',
 'If continuous coverage of an area matters, that camera needs mains power '
 'and confirmed ONVIF - decided at purchase, not afterwards.')

) AS i (subject, verdict, requires, caveat, evidence_url, checked_on, notes)
WHERE NOT EXISTS (
    SELECT 1 FROM compatibility_rules c WHERE c.subject = i.subject
);

-- Record the camera itself. integration_path stays NULL: owned, but nothing
-- has yet proven it reaches the platform, so find_unreachable() will list it
-- alongside the water valve.
INSERT INTO properties (name, kind)
VALUES ('Home', 'home')
ON CONFLICT (name) DO NOTHING;

INSERT INTO equipment (
    property_id, name, category, protocol, ecosystem, status, notes
)
SELECT
    p.id,
    'Solar dual-linkage security camera',
    'security',
    'wifi',
    NULL,
    'active',
    'Unbranded solar dual-lens unit: fixed wide-angle lens plus motorised '
    'PTZ tracking lens, dual-lens linkage where the fixed view cues the PTZ '
    'onto a moving target. Dual PIR with humanoid AI tracking, roughly '
    '10-15 m motion range. Connectivity is Wi-Fi or 4G LTE via SIM. Powered '
    'by an independent monocrystalline solar panel charging an internal '
    'lithium battery. No manual or guide located; no vendor documentation '
    'found. NOT a Sonoff product and not Zigbee. '
    'TO IDENTIFY: note which phone app it pairs with (Tuya/SmartLife, '
    'CloudEdge, iCSee, V380, UBox are the usual suspects) - the app names '
    'the ecosystem and determines whether any local access exists. Then '
    'check the app settings for an ONVIF/RTSP toggle, and scan the camera '
    'IP for ports 554 (RTSP) and 8000/80 (ONVIF).'
FROM properties p
WHERE p.name = 'Home'
  AND NOT EXISTS (
      SELECT 1 FROM equipment e
      WHERE e.name = 'Solar dual-linkage security camera'
        AND e.property_id = p.id
  );

COMMIT;
