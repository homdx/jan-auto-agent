#!/bin/bash

# Define base paths
BASE_DIR="$HOME/Downloads/crypie/test-openrouter"
AGENT_DIR="$BASE_DIR/jan-auto-agent"
PLANNED_DIR="$HOME/Downloads/crypie/planned"
GROUND_DIR="$HOME/Downloads/crypie"
REPORT_FILE="$AGENT_DIR/report.txt"

# ---------------------------------------------------------
# MODEL LISTS
# ---------------------------------------------------------

# Option 1: Models to replace under "kilo ai model"
MODELS_KILO=(
    "nvidia/nemotron-3-ultra-550b-a55b:free"
    "tencent/hy3:free"
    "dots-studio/dots-3-note-preview:free"
    "nvidia/nemotron-3-super-120b-a12b:free"
    "stepfun/step-3.7-flash:free"
    "poolside/laguna-s-2.1:free"
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
    "cohere/north-mini-code:free"
    "poolside/laguna-xs-2.1:free"
    "nvidia/nemotron-3.5-lightning:free"
    "liquid/lfm-2.5-2.6b:free"
    "nvidia/nemotron-3.5-content-safety:free"
)

# Option 2: Model to replace under "openrouter ai model"
MODELS_OPENROUTER=(
    "poolside/laguna-s-2.1:free"
)

# Option 3: Model to replace under "google ai model"
MODELS_GOOGLE=(
    "gemini-3.1-flash-lite"
)

# ---------------------------------------------------------
# TEST SCENARIOS Definitions
# Format: "Archive_Name|Extracted_Folder_Name|Ground_Truth_File|Scenario_Name"
# ---------------------------------------------------------
SCENARIOS=(
    "google6-gemini-3-1.tar.gz|test1|GROUND-TRUTH-GOOGLE.md|GOOGLE"
    "mistral-large-task-created.tar.gz|test2|GROUND-TRUTH-mistral-large.md|MISTRAL"
    "nvidia-collected.tar.gz|test1|GROUND-TRUTH-NVIDIA.md|NVIDIA"
    "test-deepseek-pro-bynara.tar.gz|test1|GROUND-TRUTH-BYNARA_deepseek_pro.md|DEEPSEEK"
)

# ---------------------------------------------------------
# SANITY CHECK: agents_128k.ini location
# If the config isn't where AGENT_DIR expects it, but we were
# launched from a folder that DOES have it, just warn and use
# the launch folder as AGENT_DIR instead of failing.
# ---------------------------------------------------------
STARTUP_DIR="$(pwd)"

if [ ! -f "$AGENT_DIR/agents_128k.ini" ]; then
    if [ -f "$STARTUP_DIR/agents_128k.ini" ]; then
        echo "WARNING: agents_128k.ini not found in expected AGENT_DIR ($AGENT_DIR)."
        echo "WARNING: Found it in the startup directory instead ($STARTUP_DIR). Using that as AGENT_DIR."
        AGENT_DIR="$STARTUP_DIR"
        REPORT_FILE="$AGENT_DIR/report.txt"
    else
        echo "WARNING: agents_128k.ini not found in AGENT_DIR ($AGENT_DIR) or startup dir ($STARTUP_DIR)."
        echo "WARNING: Continuing anyway, but the model-replacement sed step will likely fail."
    fi
fi

# Ensure we start in the agent directory
cd "$AGENT_DIR" || exit 1

echo "Starting Automated Model Testing Batch..."
echo "Results will be appended to: $REPORT_FILE"
echo ""

# ---------------------------------------------------------
# MAIN TESTING FUNCTION
# ---------------------------------------------------------
run_test_suite() {
    local MODEL="$1"
    local TARGET_LINE="$2"

    echo "=========================================================="
    echo "Configuring test for model: $MODEL"
    echo "Targeting INI section: $TARGET_LINE"
    echo "=========================================================="

    # Update agents_128k.ini for the specific target string
    sed -i "/$TARGET_LINE/{n;s|model = .*|model = $MODEL|}" agents_128k.ini

    # Loop: Run the 4 scenarios for the current model
    for SCENARIO in "${SCENARIOS[@]}"; do
        # Parse scenario variables using IFS
        IFS='|' read -r ARCHIVE UNPACK_DIR GROUND_FILE SCENARIO_NAME <<< "$SCENARIO"

        echo ">>> Testing $MODEL against scenario: $SCENARIO_NAME"

        # 1. Clean up previous tests
        cd "$BASE_DIR" || exit 1
        rm -rf test1 test2

        # 2. Extract the archive
        tar -xf "$PLANNED_DIR/$ARCHIVE"

        # 3. Handle folder renaming
        if [ "$UNPACK_DIR" == "test2" ]; then
            mv test2 test1
        fi

        # 4. Return to the agent folder
        cd "$AGENT_DIR" || exit 1

        # 5. Run the main agent process (Main process output colors are kept on screen, but not piped)
        time LLM_DEBUG=1 proxychains4 python3 main.py --validate-plan --base ../test1 --config agents_128k.ini

        # 6. Evaluate output, strip ANSI colors using sed, and write to temporary file
        #    The sed command 's/\x1b\[[0-9;]*[a-zA-Z]//g' hunts down and deletes terminal color codes.
        python3 check_improvements.py ../test1 --repo . --ground-truth "$GROUND_DIR/$GROUND_FILE" | sed 's/\x1b\[[0-9;]*[a-zA-Z]//g' > report.tmp

        # 7. Append final results to the primary report.txt
        echo "tested model $MODEL (Scenario: $SCENARIO_NAME)" >> "$REPORT_FILE"
        cat report.tmp >> "$REPORT_FILE"
        echo "--------------" >> "$REPORT_FILE"

        echo "Finished scenario $SCENARIO_NAME for $MODEL."
        echo ""
    done
}

# ---------------------------------------------------------
# EXECUTION
# ---------------------------------------------------------

# Group 1 Execution (Target: "kilo ai model")
for MODEL in "${MODELS_KILO[@]}"; do
    run_test_suite "$MODEL" "kilo ai model"
done

# Group 2 Execution (Target: "openrouter ai model")
for MODEL in "${MODELS_OPENROUTER[@]}"; do
    run_test_suite "$MODEL" "openrouter ai model"
done

# Group 3 Execution (Target: "google ai model")
for MODEL in "${MODELS_GOOGLE[@]}"; do
    run_test_suite "$MODEL" "google ai model"
done

echo "All tests complete! Check the final, color-free output in $REPORT_FILE."
