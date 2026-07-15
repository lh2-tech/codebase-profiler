#!/usr/bin/env python3
"""Generate a non-technical PDF setup guide for Codebase Profiler."""

from __future__ import annotations

from pathlib import Path

from fpdf import FPDF


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "USER_GUIDE.pdf"


class GuidePDF(FPDF):
    def header(self) -> None:
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(90, 100, 120)
        self.cell(0, 8, "LH2 Codebase Profiler - Setup Guide", align="R", new_x="LMARGIN", new_y="NEXT")
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
    pdf.multi_cell(0, 8, "Easy setup guide for non-technical users", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.body(
        "This guide explains how to install and run the Codebase Profiler using Docker. "
        "The tool analyses repositories and creates a summary spreadsheet plus a zip archive. "
        "Source code is not stored in the output - only metadata such as languages, "
        "activity, and pull request information."
    )
    pdf.body("Repository: https://github.com/lh2-tech/codebase-profiler")

    pdf.section_title("Part 1 - What you need first")
    pdf.sub_title("Install Docker Desktop (one-time)")
    pdf.body("Docker runs the application for you so you do not need to install Python manually.")
    pdf.sub_title("Windows")
    pdf.numbered(1, "Go to https://www.docker.com/products/docker-desktop/")
    pdf.numbered(2, "Download Docker Desktop for Windows.")
    pdf.numbered(3, "Install it and restart the computer if asked.")
    pdf.numbered(4, "Open Docker Desktop and wait until it says it is running.")
    pdf.sub_title("Mac")
    pdf.numbered(1, "Go to https://www.docker.com/products/docker-desktop/")
    pdf.numbered(2, "Download Docker Desktop for Mac.")
    pdf.numbered(3, "Install and open Docker Desktop.")
    pdf.numbered(4, "Wait until Docker Desktop is running.")

    pdf.section_title("Part 2 - Download the tool (one-time)")
    pdf.body("Open Command Prompt on Windows or Terminal on Mac.")
    pdf.body("Go to the folder where you want the tool, for example Documents.")
    pdf.body("Run these commands one at a time:")
    pdf.code_line("git clone https://github.com/lh2-tech/codebase-profiler.git")
    pdf.code_line("cd codebase-profiler")
    pdf.body(
        "If git is not installed: download Git from https://git-scm.com/download/win on Windows. "
        "On Mac, install Xcode command line tools or Git from the same website."
    )

    pdf.add_page()
    pdf.section_title("Part 3 - Set up your access tokens (one-time)")
    pdf.body("The tool needs permission to read repositories from GitHub or GitLab.")
    pdf.numbered(1, "In the codebase-profiler folder, find the file named tokens.example.")
    pdf.numbered(2, "Copy it and rename the copy to tokens (remove .example).")
    pdf.numbered(3, "Open tokens in Notepad on Windows or TextEdit on Mac.")
    pdf.numbered(4, "Add your real token values and save the file.")
    pdf.body("Example:")
    pdf.code_line("github-data-token=ghp_PASTE_YOUR_GITHUB_TOKEN_HERE")
    pdf.code_line("gitlab_token=glpat_PASTE_YOUR_GITLAB_TOKEN_HERE")
    pdf.body("Never share the tokens file. It contains passwords.")
    pdf.body(
        "Important: In the website, the field GitHub token key must exactly match the name "
        "before the equals sign in your tokens file. For example, if the file says "
        "github-data-token=..., type github-data-token in that field."
    )
    pdf.body("For GitLab, the default key name is gitlab_token.")

    pdf.section_title("Part 4 - Optional: local repository folder (offline mode only)")
    pdf.body("Only do this if you will use Already cloned here.")
    pdf.numbered(1, "Copy .env.example to a new file named .env")
    pdf.numbered(2, "Open .env and set LOCAL_REPOS_DIR to the folder on your computer that contains git clones.")
    pdf.body("Windows example:")
    pdf.code_line("LOCAL_REPOS_DIR=C:\\Users\\YourName\\customer-repos")
    pdf.body("Mac example:")
    pdf.code_line("LOCAL_REPOS_DIR=/Users/YourName/customer-repos")
    pdf.body(
        "Each repository inside that folder must be a full git clone, not a shallow copy. "
        "Do not use git clone --depth."
    )

    pdf.section_title("Part 5 - Start the app (every time you use it)")
    pdf.numbered(1, "Make sure Docker Desktop is running.")
    pdf.numbered(2, "Open Command Prompt or Terminal.")
    pdf.numbered(3, "Go to the codebase-profiler folder.")
    pdf.code_line("cd path\\to\\codebase-profiler")
    pdf.numbered(4, "Start the application:")
    pdf.code_line("docker compose up --build")
    pdf.numbered(5, "The first run may take several minutes.")
    pdf.numbered(6, "Leave the command window open while the app is running.")
    pdf.numbered(7, "Open your web browser and go to:")
    pdf.code_line("http://localhost:8766")
    pdf.body("You should see the LH2 logo and the Repository Evidence Extractor page.")
    pdf.body("To stop the app: press Ctrl+C in the command window, then run:")
    pdf.code_line("docker compose down")

    pdf.add_page()
    pdf.section_title("Part 6 - Using the website")
    pdf.sub_title("Choose your mode")
    pdf.bullet("Already cloned here - use when repositories are already on your computer.")
    pdf.bullet("Hosted platform - use when repositories are on GitHub or GitLab.")

    pdf.sub_title("Option A - Already cloned here (offline)")
    pdf.numbered(1, "Select Already cloned here.")
    pdf.numbered(2, "In Folder holding full local clones, type: /data/repos")
    pdf.body("Always use /data/repos in Docker. Do not type your Windows C:\\ path in this field.")
    pdf.numbered(3, "Click Load repositories and wait for Loading to finish.")
    pdf.numbered(4, "Optional: tick only the repositories you want.")
    pdf.body("Leave all unchecked to include every repository in the folder.")
    pdf.numbered(5, "Use Select all or Clear if needed.")
    pdf.numbered(6, "Leave Parallel workers at 4 unless told otherwise.")
    pdf.numbered(7, "Optional: enable LLM analysis and paste an OpenAI API key. This needs internet.")
    pdf.numbered(8, "Click Create output.")
    pdf.numbered(9, "Keep the browser tab open until the status shows Completed successfully.")

    pdf.sub_title("Option B - Hosted platform (GitHub or GitLab)")
    pdf.numbered(1, "Select Hosted platform.")
    pdf.numbered(2, "Choose GitHub or GitLab as the platform.")
    pdf.numbered(3, "In Path to token file, type: /app/tokens")
    pdf.numbered(4, "Enter the token key name exactly as it appears in your tokens file.")
    pdf.numbered(5, "Click Load organisations for GitHub or Load groups for GitLab.")
    pdf.numbered(6, "Choose an organisation or group from the dropdown.")
    pdf.numbered(7, "Wait while repositories load. Large organisations may take a minute.")
    pdf.numbered(8, "Optional: tick specific repositories, or leave all unchecked for the whole org.")
    pdf.numbered(9, "Leave Parallel workers at 4 unless told otherwise.")
    pdf.numbered(10, "Optional: enable LLM analysis and paste an OpenAI API key.")
    pdf.numbered(11, "Click Create output.")
    pdf.numbered(12, "Keep the browser tab open until completion.")

    pdf.add_page()
    pdf.section_title("Part 7 - Get your results")
    pdf.body("When the status shows Completed successfully:")
    pdf.bullet("Click Download summary to get the spreadsheet.")
    pdf.bullet("Click Download archive zip to get the full metadata package.")
    pdf.bullet(
        "Open the outputs folder on your computer: codebase-profiler\\outputs\\raw-extracts "
        "on Windows or codebase-profiler/outputs/raw-extracts on Mac."
    )
    pdf.body("Each run creates a dated folder and a zip file next to it.")

    pdf.section_title("Part 8 - Common problems")
    pdf.bullet("Page will not open: check Docker Desktop is running. Try http://127.0.0.1:8766")
    pdf.bullet(
        "Token errors: make sure the file is named tokens and is a file, not a folder. "
        "Recreate it from tokens.example if needed."
    )
    pdf.bullet("No organisations listed: check the token is valid and the token key name matches the UI field.")
    pdf.bullet(
        "No local repositories found: check LOCAL_REPOS_DIR in .env and use /data/repos in the website."
    )
    pdf.bullet("Loading forever: check internet for hosted mode and read messages in the command window.")
    pdf.bullet("Port already in use: in .env set UI_PORT=8771 and open http://localhost:8771")

    pdf.section_title("Quick checklist")
    checklist = [
        "Docker Desktop installed and running",
        "Repository cloned from GitHub",
        "tokens file created and filled in",
        "Optional .env updated for offline local clones",
        "docker compose up --build is running",
        "Browser open at http://localhost:8766",
        "Repositories loaded and Create output clicked",
        "Summary and zip downloaded from the Results section",
    ]
    for item in checklist:
        pdf.bullet(f"[ ] {item}")

    pdf.ln(4)
    pdf.body("Support: https://github.com/lh2-tech/codebase-profiler")
    pdf.body("LH2: https://www.lh2.ai")

    pdf.output(str(OUTPUT))
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    build_pdf()
