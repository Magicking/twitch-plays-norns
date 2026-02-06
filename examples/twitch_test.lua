-- twitch_test
-- simple script to test twitch-plays-norns mod
--
-- shows key presses and encoder turns on screen
-- useful for verifying the mod is receiving commands

local screen_dirty = true
local last_key = {0, 0, 0}
local last_enc = {0, 0, 0}
local enc_values = {0, 0, 0}
local message = ""
local message_time = 0

function init()
  screen.aa(1)
  screen.font_size(8)

  -- redraw clock
  clock.run(function()
    while true do
      clock.sleep(1/15)
      if screen_dirty then
        redraw()
        screen_dirty = false
      end
      -- fade key indicators
      for i = 1, 3 do
        if last_key[i] > 0 then
          last_key[i] = last_key[i] - 0.1
          screen_dirty = true
        end
      end
      -- fade message
      if message_time > 0 then
        message_time = message_time - 1/15
        if message_time <= 0 then
          message = ""
        end
        screen_dirty = true
      end
    end
  end)

  print("twitch_test ready")
  print("waiting for commands...")
end

function key(n, z)
  last_key[n] = z == 1 and 1 or 0.5
  message = "K" .. n .. (z == 1 and " pressed" or " released")
  message_time = 2
  screen_dirty = true
end

function enc(n, d)
  enc_values[n] = enc_values[n] + d
  last_enc[n] = d
  message = "E" .. n .. " delta: " .. d
  message_time = 2
  screen_dirty = true
end

function redraw()
  screen.clear()

  -- title
  screen.level(15)
  screen.move(64, 10)
  screen.text_center("TWITCH PLAYS TEST")

  -- encoder values
  screen.level(10)
  for i = 1, 3 do
    local x = 20 + (i-1) * 44
    screen.move(x, 28)
    screen.text_center("E" .. i)
    screen.move(x, 38)
    screen.level(15)
    screen.text_center(tostring(enc_values[i]))

    -- show last delta
    if last_enc[i] ~= 0 then
      screen.level(5)
      screen.move(x, 46)
      local sign = last_enc[i] > 0 and "+" or ""
      screen.text_center(sign .. last_enc[i])
    end
  end

  -- key indicators
  for i = 1, 3 do
    local x = 20 + (i-1) * 44
    local level = math.floor(last_key[i] * 15)
    screen.level(level)
    screen.rect(x - 8, 52, 16, 10)
    screen.fill()
    screen.level(15)
    screen.move(x, 60)
    screen.text_center("K" .. i)
  end

  -- message
  if message ~= "" then
    screen.level(math.floor(message_time * 7.5))
    screen.move(64, 64)
    screen.text_center(message)
  end

  screen.update()
end
