"""Names shared by the current complete-toolkit build and release pipeline."""
from workbench import __version__

VERSION = __version__
TAG = f'v{VERSION}'
ACCEPTANCE_BRANCH = 'fix/interaction-lifecycle-0.6.1'
PUBLISH_BRANCH = f'release/validated-{VERSION}'
ACCEPTANCE_WORKFLOW = '.github/workflows/desktop-acceptance.yml'
WINDOWS_DIRECTORY = f'Figure_Workbench_{VERSION}_Windows_x64'
SOURCE_DIRECTORY = f'Figure_Workbench_{VERSION}_Source'
FULL = f'Figure_Workbench_{VERSION}_Windows_Full.zip'
SOURCE = f'{SOURCE_DIRECTORY}.zip'
BROWSER = f'Figure_Workbench_{VERSION}_Browser.html'
CPU_INTERMEDIATE = f'Figure_Workbench_{VERSION}_Cellpose_CPU.zip'
QA = f'Figure_Workbench_{VERSION}_QA_Evidence.zip'
RECEIPT = 'RELEASE_ACCEPTANCE.json'
CHECKSUMS = 'SHA256SUMS'
PACKAGES = frozenset({FULL, SOURCE, BROWSER})
ATTACHMENTS = PACKAGES | {QA, RECEIPT, CHECKSUMS}
