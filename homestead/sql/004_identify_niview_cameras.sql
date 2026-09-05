-- The solar dual-lens cameras are identified: NiView app, LS VISION OEM,
-- built on an Ingenic SoC.
--
-- This settles the 'unknown' from 003 in one direction and opens a route in
-- another. It also retires the battery duty-cycle caveat for this deployment:
-- the solar panels are not being installed, every camera runs on permanent
-- external power, so the sleep-between-PIR-events limitation does not apply
-- here. Recording is to on-board SD card; cloud storage is neither used nor
-- wanted.
--
--   psql -d homestead -f homestead/sql/004_identify_niview_cameras.sql

BEGIN;

-- Retire the placeholder now that the vendor is known. Guarded so a database
-- that never saw 003's row is unaffected.
DELETE FROM compatibility_rules
WHERE subject = 'Solar dual-linkage camera (unbranded, dual-lens PIR, WiFi/4G)'
  AND EXISTS (
      SELECT 1 FROM compatibility_rules c
      WHERE c.subject = 'NiView / LS VISION dual-lens cameras (Ingenic SoC)'
  );

UPDATE compatibility_rules
SET subject      = 'NiView / LS VISION dual-lens cameras (Ingenic SoC)',
    verdict      = 'works_with_caveats',
    requires     = 'Stock firmware is cloud-app only with SD recording. For '
                   'any local stream, ONVIF/RTSP or MQTT, the route is '
                   'thingino open-source firmware for Ingenic SoC cameras.',
    caveat       = 'The NiView app is a proprietary P2P cloud client. Ingenic '
                   'battery-class cameras on stock firmware typically expose '
                   'no RTSP and no ONVIF, so Home Assistant, go2rtc and '
                   'Frigate have nothing to connect to. Footage lands on the '
                   'camera SD card and is reachable only through the app. '
                   'Reflashing is invasive and model-specific: confirm the '
                   'exact SoC (T20/T30/T31 class) against the thingino '
                   'supported-camera list before attempting it, and expect to '
                   'open the housing for serial access. A failed flash bricks '
                   'the camera.',
    evidence_url = 'https://github.com/themactep/thingino-firmware',
    checked_on   = DATE '2026-09-04',
    notes        = 'Identified from the pairing app: NiView 2.1.6. LS VISION '
                   'is the OEM/ODM behind NiView-branded solar cameras, built '
                   'on Ingenic SoCs. thingino provides RTSP, ONVIF, MQTT and '
                   'HTTP snapshots with no cloud contact, and ONVIF is on by '
                   'default - it is the only free and open-source path to '
                   'local control of these cameras. Stock firmware offers '
                   'none. NOT a Sonoff device; the Sonoff ONVIF finding never '
                   'applied here. '
                   'DEPLOYMENT NOTE: solar is not being installed at this '
                   'site - all cameras run on permanent external power, so '
                   'the battery sleep limitation is not a constraint for us.'
WHERE subject = 'Solar dual-linkage camera (unbranded, dual-lens PIR, WiFi/4G)';

-- Insert instead, for a database that never applied 003.
INSERT INTO compatibility_rules
    (subject, verdict, requires, caveat, evidence_url, checked_on, notes)
SELECT i.* FROM (VALUES
('NiView / LS VISION dual-lens cameras (Ingenic SoC)',
 'works_with_caveats',
 'Stock firmware is cloud-app only with SD recording. For any local stream, '
 'ONVIF/RTSP or MQTT, the route is thingino open-source firmware for Ingenic '
 'SoC cameras.',
 'The NiView app is a proprietary P2P cloud client. Ingenic battery-class '
 'cameras on stock firmware typically expose no RTSP and no ONVIF. '
 'Reflashing is invasive and model-specific; a failed flash bricks the '
 'camera.',
 'https://github.com/themactep/thingino-firmware',
 DATE '2026-09-04',
 'Identified from the pairing app: NiView 2.1.6, LS VISION OEM, Ingenic SoC.')
) AS i (subject, verdict, requires, caveat, evidence_url, checked_on, notes)
WHERE NOT EXISTS (
    SELECT 1 FROM compatibility_rules c
    WHERE c.subject = 'NiView / LS VISION dual-lens cameras (Ingenic SoC)'
);

-- The general battery-camera rule stays on record for future purchases, but
-- note it does not bind this deployment.
UPDATE compatibility_rules
SET notes = COALESCE(notes, '') ||
            ' NOT A CONSTRAINT AT THIS SITE: the NiView cameras here run on '
            'permanent external power rather than solar, so they do not sleep '
            'between events. This rule still applies to any future '
            'battery-only purchase.'
WHERE subject = 'Solar or battery-powered PIR cameras (general)'
  AND notes NOT LIKE '%NOT A CONSTRAINT AT THIS SITE%';

-- Drop the stale equipment row before renaming, for the same reason as the
-- compatibility rule above: 003 re-inserts under the old name whenever it is
-- re-applied, because its guard cannot know about a rename that happens here.
-- Renaming without this delete would leave two rows for one camera.
DELETE FROM equipment
WHERE name = 'Solar dual-linkage security camera'
  AND EXISTS (
      SELECT 1 FROM equipment e
      WHERE e.name = 'NiView dual-lens security camera'
  );

-- Update the equipment record with what is now known.
UPDATE equipment
SET name      = 'NiView dual-lens security camera',
    ecosystem = 'niview',
    notes     = 'Dual-lens unit: fixed wide-angle plus motorised PTZ tracking '
                'lens with dual-lens linkage. Dual PIR with humanoid AI '
                'tracking, roughly 10-15 m range. Wi-Fi (2.4 GHz only) or 4G '
                'LTE. Pairs with the NiView app, version 2.1.6 - LS VISION is '
                'the OEM and the hardware is Ingenic SoC based. Records to an '
                'on-board SD card; cloud storage is not used and not wanted. '
                'Sold as solar-capable, but the panels are NOT being '
                'installed: every unit runs on permanent external power to '
                'stay continuously active. '
                'STATUS: one unit active, the rest currently offline and '
                'believed unpowered. '
                'LOCAL ACCESS: none on stock firmware. Before assuming '
                'otherwise, scan the active camera for ports 554 (RTSP) and '
                '80/8000 (ONVIF); expect both closed. The open-source route '
                'is thingino firmware, which requires confirming the exact '
                'Ingenic SoC against its supported list first.'
WHERE name = 'Solar dual-linkage security camera';

COMMIT;
