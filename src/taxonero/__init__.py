import os

# Mac (Apple GPU, MPS): an operation Metal cannot do runs on the CPU instead of stopping the analysis.
# To be set before the first import of torch.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
