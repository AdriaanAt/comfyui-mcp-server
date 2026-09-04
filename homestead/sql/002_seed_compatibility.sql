-- Compatibility findings established the hard way, recorded so they are never
-- rediscovered at the cost of another unusable purchase.
--
-- Every row carries evidence. "Someone said so" is not a verdict; if there is
-- no link or no first-hand test, the verdict is 'unknown'.
--
--   psql -U homestead -d homestead -f homestead/sql/002_seed_compatibility.sql

BEGIN;

INSERT INTO compatibility_rules
    (subject, verdict, requires, caveat, evidence_url, checked_on, notes)
VALUES

-- The purchase that started all of this.
('SONOFF Zigbee Smart Water Valve',
 'works_with_caveats',
 'A Zigbee coordinator running Zigbee2MQTT or ZHA, plus an MQTT broker.',
 'The dongle alone is not a hub. Bought without coordinator software, this '
 'valve is simply unreachable - which is exactly what happened.',
 'https://www.zigbee2mqtt.io/',
 DATE '2026-09-04',
 'Vendor copy advertises open-source support "via ZBDongle-E", a different '
 'and older dongle than the MG24 that was actually bought.'),

-- The dongle, and the trap underneath it.
('SONOFF Dongle Plus MG24 (EFR32MG24)',
 'works_with_caveats',
 'Zigbee2MQTT with adapter: ember. Run it on Linux, or on WSL2 - not native '
 'Windows.',
 'On native Windows this dongle hits an endless "Waiting for RSTACK" reset '
 'loop ending in HOST_FATAL_ERROR. The cause is Windows serial handling of '
 'the CP2102N chip, not the firmware, so reflashing does not fix it. Under '
 'WSL2 the Linux cp210x driver takes over and the bug does not apply.',
 'https://github.com/Koenkk/zigbee2mqtt/issues/28743',
 DATE '2026-09-04',
 'Distinct from the ZBDongle-E (EFR32MG21). Community firmware for the MG24 '
 'was still unreleased as of early 2026. Use the ember driver: ezsp is '
 'deprecated, zstack is for the CC2652-based ZBDongle-P.'),

('Zigbee2MQTT on native Windows',
 'blocked',
 'Run under WSL2 with usbipd-win, or on a Linux host.',
 'CP2102N serial handling on Windows causes adapter reset loops. Marked '
 'fixed-in-dev upstream but not reliably released.',
 'https://github.com/Koenkk/zigbee2mqtt/issues/31281',
 DATE '2026-09-04',
 'Reflashing, baud changes, port changes and ezsp-vs-ember were all tried by '
 'the upstream reporter; none worked.'),

('Home Assistant on Windows',
 'blocked',
 'Home Assistant OS on a VM or dedicated box, or Home Assistant Container '
 'under Docker/WSL2.',
 'There is no native Windows install. Core and Supervised were deprecated, '
 'leaving only HA OS and HA Container - so any Windows setup is a VM or a '
 'container regardless.',
 'https://www.home-assistant.io/installation/',
 DATE '2026-09-04',
 NULL),

-- Cameras: the trap that did not materialise.
('SONOFF cameras (ONVIF/RTSP)',
 'works_with_caveats',
 'Enable ONVIF/RTSP in eWeLink under Device Settings > More Settings, then '
 'add via ONVIF, go2rtc or Frigate.',
 'RTSP is off by default and must be switched on through the vendor app once. '
 'Confirm the specific model supports it before buying more - solar models '
 'vary and cloud-only variants exist.',
 'https://sonoff.tech/en-us/blogs/news/how-to-add-security-camera-to-home-assistant',
 DATE '2026-09-04',
 'Community reports indicate the stream keeps working with the camera''s '
 'internet access blocked, which makes local-only recording viable.'),

-- Wi-Fi devices reach MQTT by a different road than the Zigbee ones.
('SONOFF 4CHR3 / 4CHPROR3',
 'works_with_caveats',
 'Flash Tasmota or ESPHome for native MQTT, or fall back to eWeLink-LAN mode.',
 'Wi-Fi, not Zigbee - the Zigbee coordinator will never see it. Newer units '
 'may ship with DIY mode locked, in which case flashing needs serial access.',
 'https://tasmota.github.io/docs/devices/Sonoff-4CH/',
 DATE '2026-09-04',
 NULL),

('SONOFF MTS22 smart timer',
 'unknown',
 'Confirm the radio before planning an integration path.',
 'Protocol not yet verified first-hand. If it is Tuya-based Wi-Fi, a Tuya '
 'local bridge applies; if eWeLink, treat it like the 4CHR3.',
 NULL,
 DATE '2026-09-04',
 'Deliberately left unknown rather than guessed - guessing is what produced '
 'the unusable valve.'),

-- Solar, recorded before money is spent rather than after.
('Per-panel solar monitoring',
 'works_with_caveats',
 'Microinverters or per-panel optimisers.',
 'A string inverter reports per MPPT string, not per panel. Watching each '
 'panel''s contribution as panels are added is a purchase-time hardware '
 'decision that cannot be recovered in software afterwards.',
 NULL,
 DATE '2026-09-04',
 'Relevant to the Deye inverter and the home_assistant_solarman integration.'),

-- Platform glue.
('Open WebUI as a Home Assistant conversation agent',
 'works_with_caveats',
 'HACS, plus the ha-openwebui-conversation integration and a cloned model in '
 'Open WebUI.',
 'This agent cannot control the house - it answers, it does not call '
 'services. Keep built-in Assist primary with "Prefer handling commands '
 'locally" enabled and let Open WebUI take what Assist cannot match. Base '
 'models are unsupported; clone one first.',
 'https://github.com/TheRealPSV/ha-openwebui-conversation',
 DATE '2026-09-04',
 'For actual device control use the other direction: the Home Assistant MCP '
 'Server integration.'),

('Home Assistant MCP Server integration',
 'works',
 'Add the Model Context Protocol Server integration, then register '
 '/api/mcp as a tool server in Open WebUI.',
 'Exposure is opt-in per entity under Settings > Voice assistants > Exposed '
 'entities. Keep that list tight - expose the valve and lights, not the '
 'alarm.',
 'https://www.home-assistant.io/integrations/mcp_server/',
 DATE '2026-09-04',
 'Streamable HTTP with token auth. Open WebUI speaks this natively, so no '
 'proxy or custom glue is needed.'),

('HACS (Home Assistant Community Store)',
 'works',
 NULL,
 NULL,
 'https://hacs.xyz/',
 DATE '2026-09-04',
 'Free and open source. Earlier notes recorded it as possibly paid; it is '
 'not.')

ON CONFLICT DO NOTHING;

COMMIT;
