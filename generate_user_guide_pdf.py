#!/usr/bin/env python3
"""Generate a client-facing PDF setup guide for Codebase Profiler."""

from __future__ import annotations

from pathlib import Path

from fpdf import FPDF


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "USER_GUIDE.pdf"


class GuidePDF(FPDF):
    def header(self) -> None:
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(90, 100, 120)
        self.cell(0, 8, "LH2 Codebase Profiler | Client Setup Guide", align="R", new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def footer(self) -> None:
        self.set_y(-15)
        self.set_font("Helvetica", "I", 9)
        self.set_text_color(120, 120, 120)
        self.cell(0, 10, f"Page {self.page_no()}", align="C")

    def section_title(self, title: str) -> None:
        self.ln(4)
        self.set_x(self.l_margin)
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(23, 32, 51)
        self.multi_cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def sub_title(self, title: str) -> None:
        self.ln(2)
        self.set_x(self.l_margin)
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(29, 78, 216)
        self.multi_cell(0, 7, title, new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def branch_title(self, title: str) -> None:
        self.ln(3)
        self.set_x(self.l_margin)
        self.set_font("Helvetica", "B", 12)
        self.set_text_color(180, 83, 9)
        self.multi_cell(0, 7, title, new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def body(self, text: str) -> None:
        self.set_x(self.l_margin)
        self.set_font("Helvetica", "", 10)
        self.set_text_color(40, 40, 40)
        self.multi_cell(0, 5.5, text, new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def bullet(self, text: str) -> None:
        self.set_x(self.l_margin)
        self.set_font("Helvetica", "", 10)
        self.set_text_color(40, 40, 40)
        self.multi_cell(0, 5.5, f"- {text}", new_x="LMARGIN", new_y="NEXT")

    def numbered(self, number: int, text: str) -> None:
        self.set_x(self.l_margin)
        self.set_font("Helvetica", "", 10)
        self.set_text_color(40, 40, 40)
        self.multi_cell(0, 5.5, f"{number}. {text}", new_x="LMARGIN", new_y="NEXT")

    def code_line(self, text: str) -> None:
        self.set_x(self.l_margin)
        self.set_font("Courier", "", 9)
        self.set_fill_color(245, 247, 251)
        self.set_text_color(20, 20, 20)
        self.multi_cell(0, 6, f"  {text}", fill=True, new_x="LMARGIN", new_y="NEXT")
        self.ln(1)


def build_pdf() -> None:
    pdf = GuidePDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 22)
    pdf.set_text_color(23, 32, 51)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, 10, "LH2 Codebase Profiler", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 13)
    pdf.set_text_color(83, 96, 120)
    pdf.multi_cell(0, 8, "Client setup guide", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.body(
        "This document explains how to run the LH2 Codebase Profiler on your own computer. "
        "The tool creates a summary spreadsheet and a zip file of repository metadata. "
        "It does not store full source code in the output."
    )
    pdf.body("Tool download: https://github.com/lh2-tech/codebase-profiler")

    pdf.section_title("Before you start: pick your workflow")
    pdf.body("There are two ways to use the tool. Choose ONE path below. You do not need both.")
    pdf.ln(1)
    pdf.branch_title("Workflow A - Hosted platform (GitHub or GitLab)")
    pdf.body(
        "Use this when repositories live on GitHub or GitLab and you have an access token. "
        "You will set up a tokens file, then run the app and sign in through the website."
    )
    pdf.branch_title("Workflow B - Already cloned here (offline)")
    pdf.body(
        "Use this when repositories are already downloaded as full git folders on your computer. "
        "You do not need GitHub or GitLab tokens for this path. You will point the tool at a "
        "folder on your computer that contains those clones."
    )

    pdf.section_title("Step 1 - Install Docker Desktop (required for everyone)")
    pdf.sub_title("Windows")
    pdf.numbered(1, "Open your web browser and go to https://www.docker.com/products/docker-desktop/")
    pdf.numbered(2, "Download Docker Desktop for Windows and install it.")
    pdf.numbered(3, "Restart your computer if the installer asks you to.")
    pdf.numbered(4, "Open Docker Desktop from the Start menu and wait until it shows Running.")
    pdf.sub_title("Mac")
    pdf.numbered(1, "Open your web browser and go to https://www.docker.com/products/docker-desktop/")
    pdf.numbered(2, "Download Docker Desktop for Mac and install it.")
    pdf.numbered(3, "Open Docker Desktop from Applications and wait until it shows Running.")

    pdf.section_title("Step 2 - Download the tool (required for everyone)")
    pdf.sub_title("2A. Open a command window")
    pdf.body("Windows: press the Windows key, type cmd, and open Command Prompt.")
    pdf.body("Mac: press Command + Space, type Terminal, and open Terminal.")
    pdf.sub_title("2B. Go to the folder where you want the tool to live")
    pdf.body(
        "Example: your Documents folder. Type the command for your system, then press Enter."
    )
    pdf.body("Windows example:")
    pdf.code_line("cd %USERPROFILE%\\Documents")
    pdf.body("Mac example:")
    pdf.code_line("cd ~/Documents")
    pdf.sub_title("2C. Download the tool")
    pdf.code_line("git clone https://github.com/lh2-tech/codebase-profiler.git")
    pdf.sub_title("2D. Open the tool folder (important)")
    pdf.body("After cloning, you must go inside the new codebase-profiler folder:")
    pdf.body("Windows:")
    pdf.code_line("cd codebase-profiler")
    pdf.body("Mac:")
    pdf.code_line("cd codebase-profiler")
    pdf.body(
        "Tip (Windows): in File Explorer, open Documents, double-click codebase-profiler, "
        "click the address bar at the top, copy the full path (for example "
        "C:\\Users\\YourName\\Documents\\codebase-profiler), then in Command Prompt type "
        "cd , paste the path, and press Enter."
    )
    pdf.body(
        "Tip (Mac): in Finder, open Documents, right-click codebase-profiler, choose "
        "Get Info, and copy the Where path. In Terminal type cd , paste the path, press Enter."
    )
    pdf.body(
        "If git is not installed: install Git from https://git-scm.com/download/win (Windows) "
        "or https://git-scm.com/download/mac (Mac), then repeat this step."
    )

    pdf.add_page()
    pdf.section_title("Step 3 - Set up YOUR workflow (choose A or B)")
    pdf.body("Complete only the branch that matches the workflow you chose at the start.")

    pdf.branch_title("Workflow A only - Set up your GitHub / GitLab token")
    pdf.body("Skip this entire section if you are using Workflow B (offline local clones).")
    pdf.numbered(1, "Open File Explorer (Windows) or Finder (Mac).")
    pdf.numbered(2, "Go to the codebase-profiler folder you downloaded in Step 2.")
    pdf.numbered(3, "Find the file named tokens.example.")
    pdf.numbered(4, "Make a copy of that file in the same folder.")
    pdf.numbered(5, "Rename the copy to tokens (remove .example). It must be a file, not a folder.")
    pdf.numbered(6, "Open tokens with Notepad (Windows) or TextEdit (Mac).")
    pdf.numbered(7, "Paste your real token values. Example:")
    pdf.code_line("github-data-token=ghp_YOUR_GITHUB_TOKEN")
    pdf.code_line("gitlab_token=glpat_YOUR_GITLAB_TOKEN")
    pdf.numbered(8, "Save and close the file. Do not email or share this file.")
    pdf.body(
        "In the website later, the field GitHub token key must match the text before the equals "
        "sign in this file. If the file says github-data-token=, enter github-data-token in the website."
    )

    pdf.branch_title("Workflow B only - Point the tool at your local repository folder")
    pdf.body("Skip this entire section if you are using Workflow A (hosted GitHub / GitLab).")
    pdf.sub_title("3B-1. Prepare the folder that contains your git clones")
    pdf.body(
        "Put each repository inside one parent folder as a full clone (not a shallow copy). "
        "Example layout:"
    )
    pdf.code_line("C:\\CustomerRepos\\api-service")
    pdf.code_line("C:\\CustomerRepos\\web-app")
    pdf.sub_title("3B-2. Copy the full path to that parent folder")
    pdf.body("Windows:")
    pdf.numbered(1, "Open File Explorer.")
    pdf.numbered(2, "Navigate to the parent folder that contains your repository folders.")
    pdf.numbered(3, "Click the address bar at the top of the window.")
    pdf.numbered(4, "Copy the full path (example: C:\\Users\\YourName\\CustomerRepos).")
    pdf.body("Mac:")
    pdf.numbered(1, "Open Finder.")
    pdf.numbered(2, "Open the parent folder that contains your repository folders.")
    pdf.numbered(3, "Right-click the folder name and choose Get Info.")
    pdf.numbered(4, "Copy the path shown next to Where.")
    pdf.sub_title("3B-3. Tell Docker which folder to use")
    pdf.numbered(1, "In the codebase-profiler folder, find the file .env.example.")
    pdf.numbered(2, "Copy it and rename the copy to .env (starts with a dot).")
    pdf.numbered(3, "Open .env in Notepad or TextEdit.")
    pdf.numbered(4, "Set LOCAL_REPOS_DIR to the path you copied:")
    pdf.body("Windows example:")
    pdf.code_line("LOCAL_REPOS_DIR=C:\\Users\\YourName\\CustomerRepos")
    pdf.body("Mac example:")
    pdf.code_line("LOCAL_REPOS_DIR=/Users/YourName/CustomerRepos")
    pdf.numbered(5, "Save and close .env.")
    pdf.body(
        "Important: In the website you will type /data/repos (not the Windows or Mac path above). "
        "Docker maps your folder to /data/repos inside the application."
    )

    pdf.add_page()
    pdf.section_title("Step 4 - Start the application (required for everyone)")
    pdf.body("You must be inside the codebase-profiler folder in your command window.")
    pdf.body("If you are not sure, run:")
    pdf.body("Windows:")
    pdf.code_line("cd %USERPROFILE%\\Documents\\codebase-profiler")
    pdf.body("Mac:")
    pdf.code_line("cd ~/Documents/codebase-profiler")
    pdf.body("Adjust the path if you saved the tool somewhere else.")
    pdf.numbered(1, "Confirm Docker Desktop is running.")
    pdf.numbered(2, "In the command window, run:")
    pdf.code_line("docker compose up --build")
    pdf.numbered(3, "Wait until the window shows the app is running. Leave this window open.")
    pdf.numbered(4, "Open a web browser and go to:")
    pdf.code_line("http://localhost:8766")
    pdf.body("To stop later: press Ctrl+C in the command window, then run docker compose down")

    pdf.section_title("Step 5 - Run an analysis in the website")
    pdf.sub_title("Workflow A - Hosted platform")
    pdf.numbered(1, "Select Hosted platform.")
    pdf.numbered(2, "Choose GitHub or GitLab.")
    pdf.numbered(3, "Path to token file: enter /app/tokens")
    pdf.numbered(4, "Token key: enter the exact name from your tokens file.")
    pdf.numbered(5, "Click Load organisations or Load groups and wait for Loading to finish.")
    pdf.numbered(6, "Choose an organisation or group from the dropdown.")
    pdf.numbered(7, "Wait for the repository or project list to appear.")
    pdf.numbered(8, "Optional: tick specific repositories or projects. Leave all unchecked to include the full org or group.")
    pdf.numbered(9, "Click Create output and keep the browser tab open until Completed successfully.")

    pdf.sub_title("Workflow B - Already cloned here")
    pdf.numbered(1, "Select Already cloned here.")
    pdf.numbered(2, "Folder holding full local clones: enter exactly /data/repos")
    pdf.numbered(3, "Click Load repositories and wait for Loading to finish.")
    pdf.numbered(4, "Optional: tick specific repositories. Leave all unchecked to include every repo.")
    pdf.numbered(5, "Click Create output and keep the browser tab open until Completed successfully.")

    pdf.section_title("Step 6 - Collect your results")
    pdf.bullet("Click Download summary for the spreadsheet.")
    pdf.bullet("Click Download archive zip for the full package.")
    pdf.bullet("Files are also saved on your computer here:")
    pdf.body("Windows: codebase-profiler\\outputs\\raw-extracts")
    pdf.body("Mac: codebase-profiler/outputs/raw-extracts")
    pdf.body(
        "To open that folder: in File Explorer or Finder, go to the codebase-profiler folder "
        "you downloaded, then open outputs, then raw-extracts."
    )

    pdf.add_page()
    pdf.section_title("Step 7 - Common issues")
    pdf.bullet("Browser page does not open: confirm Docker Desktop is running. Try http://127.0.0.1:8766")
    pdf.bullet("tokens error: the file must be named tokens and must be a file, not a folder.")
    pdf.bullet("No organisations listed (Workflow A): check token value and token key spelling.")
    pdf.bullet("No local repos found (Workflow B): check .env path and use /data/repos in the website.")
    pdf.bullet("Command window says folder not found: run cd again with the correct full path.")

    pdf.section_title("Quick checklist")
    pdf.sub_title("Everyone")
    for item in [
        "Docker Desktop is running",
        "I downloaded codebase-profiler and opened that folder in my command window",
        "I ran docker compose up --build",
        "I opened http://localhost:8766 in my browser",
        "I clicked Create output and waited for Completed successfully",
        "I downloaded the summary and zip",
    ]:
        pdf.bullet(f"[ ] {item}")
    pdf.sub_title("Workflow A only (hosted)")
    for item in [
        "I created the tokens file from tokens.example",
        "I entered /app/tokens and the correct token key in the website",
        "I loaded organisations/groups, chose one, and optionally picked projects from that group",
    ]:
        pdf.bullet(f"[ ] {item}")
    pdf.sub_title("Workflow B only (offline)")
    for item in [
        "My repository folders are inside one parent folder on my computer",
        "I created .env from .env.example with LOCAL_REPOS_DIR set to that parent folder",
        "I entered /data/repos in the website and clicked Load repositories",
    ]:
        pdf.bullet(f"[ ] {item}")

    pdf.ln(3)
    pdf.body("Support: https://github.com/lh2-tech/codebase-profiler")
    pdf.body("LH2: https://www.lh2.ai")

    pdf.output(str(OUTPUT))
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    build_pdf()
