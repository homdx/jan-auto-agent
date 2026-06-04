--- tools/auto/controller.py
+++ tools/auto/controller.py
@@ -288,11 +288,20 @@
-            # ── Future: real task execution (AUTO-B/C) hooks here ──────
-            # e.g. self._execute_task(task)
-            # For now: mark in_progress then done as a skeleton pass-through.
             self.state.set_task_status(task["id"], STATUS_IN_PROGRESS)
-            # (Real execution would happen here)
-            self.state.set_task_status(task["id"], STATUS_DONE)
-            tasks_done += 1
-            self.state.log(f"task {task['id']} completed (skeleton)")
-            
-            if self.run_trace:
-                self.run_trace.log_task_done(task["id"])
+
+            cfg = configparser.ConfigParser()
+            if Path(self.config_path).exists():
+                cfg.read(self.config_path)
+
+            from tools.auto.inner_loop import make_inner_loop
+            inner_loop = make_inner_loop(cfg, self.base_dir)
+            result = inner_loop.run_task(task, self.base_dir)
+
+            if result.passed:
+                self.state.set_task_status(task["id"], STATUS_DONE)
+                tasks_done += 1
+                self.state.log(f"task {task['id']} completed successfully")
+                if self.run_trace:
+                    self.run_trace.log_task_done(task["id"])
+            else:
+                self.state.set_task_status(task["id"], STATUS_BLOCKED)
+                self.state.log(f"task {task['id']} failed: {result.last_feedback}")
+                if self.run_trace:
+                    self.run_trace.log_task_blocked(task["id"], result.last_feedback)
 
             # AUTO-F1: Tick progress upon task completion
@@ -305,6 +314,6 @@
                 self.metrics_stream.record_gate2(
                     task["id"],
-                    approved=True,
-                    feedback="skeleton pass",
-                    attempts=1,
+                    approved=result.passed,
+                    feedback=result.last_feedback,
+                    attempts=result.attempts_used,
                     prompt_store=self.auto_tuner.prompt_store
