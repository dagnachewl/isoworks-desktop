
"""Dialog for Primary Distillation EC Editing"""
import tkinter as tk
from tkinter import messagebox

def validate_numeric(value):
    try:
        return float(value)
    except ValueError:
        return None

def compute_weighted_average(sub_analyses):
    total_weight = 0
    weighted_sum = 0
    for sa in sub_analyses:
        ec = sa.get("ec")
        weight = sa.get("weight", 1)
        if ec is not None:
            weighted_sum += ec * weight
            total_weight += weight
    return weighted_sum / total_weight if total_weight > 0 else None

class PrimaryDistillationECDialog:
    def __init__(self, db_session, batch_id):
        self.db_session = db_session
        self.batch_id = batch_id
        self.root = tk.Tk()
        self.root.title("Primary Distillation EC")

        tk.Label(self.root, text=f"Batch: {batch_id}").grid(row=0, column=0, columnspan=2)

        # Load sub-analyses for batch
        self.sub_analyses = self.load_sub_analyses()
        self.entries = []

        for i, sa in enumerate(self.sub_analyses):
            tk.Label(self.root, text=f"SubAnalysis {sa['id']} EC:").grid(row=i+1, column=0)
            entry = tk.Entry(self.root)
            entry.insert(0, str(sa.get("ec", "")))
            entry.grid(row=i+1, column=1)
            self.entries.append((sa, entry))

        tk.Button(self.root, text="Save", command=self.save_changes).grid(row=len(self.sub_analyses)+1, column=0)

    def load_sub_analyses(self):
        # TODO: Replace with DB logic
        return [{"id": 1, "ec": 10.5, "weight": 2}, {"id": 2, "ec": 12.0, "weight": 3}]

    def save_changes(self):
        updated = []
        for sa, entry in self.entries:
            val = validate_numeric(entry.get())
            if val is None:
                messagebox.showerror("Error", f"Invalid EC for SubAnalysis {sa['id']}")
                return
            sa["ec"] = val
            updated.append(sa)

        avg_ec = compute_weighted_average(updated)
        # TODO: Save updated ECs and avg_ec to DB, update statuses
        messagebox.showinfo("Info", f"Changes saved. Weighted Avg EC: {avg_ec:.2f}")

    def run(self):
        self.root.mainloop()
