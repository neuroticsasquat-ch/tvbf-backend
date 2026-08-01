-- Hand-resolved titles for the briggsjm export (NEU-927).
--
-- Every title here needed a human decision: either several catalog rows share
-- the name, or exact match found nothing. Picks were made on premiere year,
-- language, and originating network/country, against the clear pattern that
-- this user tracks mainstream English-language TV.
--
-- Four titles are deliberately NOT resolved here -- see the bottom of the file.
--
-- Re-runnable. Load with:
--   docker exec -i tbc_postgresql_db psql -U root -d tvbf -v ON_ERROR_STOP=1 \
--     < manual_resolutions.sql

\set ON_ERROR_STOP on

INSERT INTO import_ne.show_resolution (title, show_id, source) VALUES
-- Name matched nothing: punctuation, ampersands, or a missing premiere date.
  ('Anthony Bourdain Parts Unknown',   255, 'manual'),  -- catalog has the colon
  ('G.L.O.W.',                       17869, 'manual'),  -- catalog: "GLOW"
  ('José Andrés & family in Spain',   66253, 'manual'),  -- catalog: "... and Family ..."
  ('Milk Street',                     31831, 'manual'),  -- "Milk Street Television"
  ('The Good Daughter (2026)',        75488, 'manual'),  -- premiered IS NULL, so the year filter excluded it

-- Right show existed but under a longer name, so exact match never saw it.
  ('The Agency',                      66840, 'manual'),  -- "The Agency: Central Intelligence", 2024 Paramount+

-- Several catalog rows share the name; picked by year + language + network.
  ('1983',                            38958, 'manual'),  -- 2018 Netflix (Polish original)
  ('24',                                167, 'manual'),  -- 2001 Fox, 204 eps
  ('Acapulco',                        52193, 'manual'),  -- 2021 Apple TV+
  ('Annika',                          52732, 'manual'),  -- 2021, Nicola Walker
  ('Band of Brothers',                  465, 'manual'),  -- 2001 HBO
  ('Bodyguard',                       32490, 'manual'),  -- 2018 BBC
  ('Catastrophe',                      3004, 'manual'),  -- 2015 Channel 4
  ('Chambers',                        34346, 'manual'),  -- 2019 Netflix
  ('Euphoria',                        28826, 'manual'),  -- 2019 HBO
  ('Extraordinary',                   54828, 'manual'),  -- 2023 Hulu
  ('Full Circle',                     57103, 'manual'),  -- 2023 HBO Max, Soderbergh
  ('Girls',                             139, 'manual'),  -- 2012 HBO
  ('Glitch',                           2477, 'manual'),  -- 2015 ABC Australia
  ('Hostage',                         77979, 'manual'),  -- 2025 Netflix
  ('Maniac',                          14657, 'manual'),  -- 2018 Netflix
  ('Obsession',                       61093, 'manual'),  -- 2023 Netflix
  ('One Day',                         59243, 'manual'),  -- 2024 Netflix
  ('Paradise',                        75030, 'manual'),  -- 2025 Hulu
  ('Rivals',                          64087, 'manual'),  -- 2024 Disney+
  ('Scrubs',                            532, 'manual'),  -- 2001 NBC, 182 eps
  ('Smoke',                           65649, 'manual'),  -- 2025 Apple TV+
  ('Starstruck',                      54792, 'manual'),  -- 2021 BBC Three
  ('Sugar',                           62463, 'manual'),  -- 2024 Apple TV+
  ('Suits',                             172, 'manual'),  -- 2011 USA Network, 134 eps
  ('Summertime',                      46660, 'manual'),  -- 2020 Netflix (Italian)
  ('The Affair',                        127, 'manual'),  -- 2014 Showtime
  ('The Borgias',                       515, 'manual'),  -- 2011 Showtime
  ('The Curse',                       46334, 'manual'),  -- 2023 Showtime
  ('The Diplomat',                    60213, 'manual'),  -- 2023 Netflix (not the U&alibi one)
  ('The Franchise',                   63438, 'manual'),  -- 2024 HBO
  ('The Last Frontier',               67154, 'manual'),  -- 2025
  ('The Morning Show',                41524, 'manual'),  -- 2019 Apple TV+
  ('The Office (US)',                   526, 'manual'),  -- 2005 NBC, 202 eps
  ('The Power',                       43971, 'manual'),  -- 2023 Prime Video
  ('The Recruit',                     60522, 'manual'),  -- 2022 Netflix
  ('The Traitors (UK)',               58174, 'manual'),  -- BBC One, matching the (UK) tag
  ('The Wonder Years',                 1294, 'manual'),  -- 1988 ABC
  ('Van Der Valk',                    45978, 'manual'),  -- 2020 ITV
  ('Wanderlust',                      34482, 'manual'),  -- 2018 BBC

-- UK-original vs US-remake pairs. No signal in the export either way; these
-- three were confirmed by the account holder rather than inferred.
  ('Coupling',                         1085, 'manual'),  -- UK, BBC Three 2000 (confirmed)
  ('Crashing',                         8702, 'manual'),  -- UK, Channel 4 2016 (confirmed)
  ('Shameless',                         150, 'manual'),  -- US, Showtime 2011 (confirmed)

-- Ambiguous in prod but not locally: a 2-episode CBC Gem show of the same name
-- was added to TV Maze after the local restore was taken. Resolving to the 2016
-- ABC sitcom, which is what the local run picked automatically when it was the
-- only candidate.
  ('Speechless',                      11553, 'manual')   -- 2016 ABC, 63 eps
ON CONFLICT (title) DO UPDATE
  SET show_id = EXCLUDED.show_id, source = 'manual';

-- ---------------------------------------------------------------------------
-- Still unresolved: "Can't Go Home".
--
-- No catalog row under any spelling tried -- with and without the apostrophe,
-- and as a substring. Either TV Maze lists it under a different title, or it
-- is genuinely absent from the mirror. Carries no watch marks, so the cost is
-- one missing My Shows entry. Worth mentioning to the account holder rather
-- than letting them discover it.
-- ---------------------------------------------------------------------------
