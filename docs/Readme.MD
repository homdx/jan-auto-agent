Open agents.ini in your project root.
Find the [auto] section — you'll see this commented line:

ini   # task_mode = code

Uncomment it and change the value:

ini   task_mode = docs      # for documentation review
   # or
   task_mode = creative  # for poems, stories, essays

Run as normal: python main.py --auto "<your goal>" --base <dir>

Optional — per-component prompt overrides (in the same agents.ini):
ini[architect]
system_docs = You are a senior technical writer reviewing documentation...
# system_creative = You are a creative writing editor...

[gate1]
system_docs = You are a documentation reviewer performing a false-positive check...

[validator_agent]
system = You are a documentation validator...   # overrides ALL modes
If you leave those blank, the built-in prompts kick in automatically based on task_mode. The [validator_agent] system key you already have in your config will always take priority — the mode-based prompts are only the fallback when that key is absent.