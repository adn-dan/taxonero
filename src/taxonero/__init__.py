import os

# Mac (GPU Apple, MPS) : une opération que Metal ne sait pas faire passe sur le processeur au lieu d'arrêter l'analyse.
# À poser avant le premier import de torch.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
