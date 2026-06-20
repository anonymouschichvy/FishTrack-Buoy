#!/usr/bin/env python3
import os
import json
import time
import shutil
import glob
from collections import defaultdict
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
from pathlib import Path
from datetime import datetime

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from PIL import Image
from torchvision import transforms
import faiss
import timm


# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    """Configuration for the Hybrid Fish Classification System"""
    
    # File paths
    CKPT_FILE = "model.ckpt"
    DATABASE_FILE = "database.pt"
    LABELS_FILE = "labels.json"
    INPUT_DIR = "../FISHDETECTION/output"
    OUTPUT_DIR = "data"
    OUTPUT_DIR_IMAGE = "data/output"
    OUTPUT_FILE = os.path.join(OUTPUT_DIR, "fish_analysis_results.json")
    
    # Model parameters
    DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
    EMBEDDING_DIM = 512
    BACKBONE_MODEL = 'beitv2_base_patch16_224.in1k_ft_in22k_in1k'
    
    # Prediction parameters
    TOP_K_PREDICTIONS = 5
    CONFIDENCE_THRESHOLD = 0.1
    
    # Hybrid system parameters
    USE_RETRIEVAL = True
    DIRECT_WEIGHT = 0.5
    RETRIEVAL_WEIGHT = 0.5
    RETRIEVAL_TOP_K = 10
    RETRIEVAL_MIN_SIMILARITY = 0.7
    
    # ArcFace parameters
    ARCFACE_S = 30.0
    ARCFACE_M = 0.50
    USE_SOFTMAX = False
    
    # Retrieval-only candidate parameters
    RETRIEVAL_ONLY_MAX_RATIO = 0.7
    
    # Image processing
    IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.bmp', '.tiff')
    
    # GPU acceleration for FAISS (optional)
    USE_GPU_FAISS = False


# ============================================================================
# LOGGER SETUP
# ============================================================================

def log_info(msg):
    print(f"[INFO] {msg}")

def log_warning(msg):
    print(f"[WARNING] {msg}")

def log_error(msg):
    print(f"[ERROR] {msg}")


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
@dataclass
class PredictionResult:
    """Result of a single species prediction"""
    species: str
    class_id: int
    confidence: float
    direct_score: float = 0.0
    retrieval_score: float = 0.0
    
    def to_dict(self):
        """Returns species, confidence, and both scores for JSON output"""
        return {
            'species': self.species,
            'confidence': self.confidence,
            'direct_score': self.direct_score,
            'retrieval_score': self.retrieval_score
        }

# ============================================================================
# MODEL COMPONENTS
# ============================================================================

class HybridFishModel(nn.Module):
    """Fixed model with proper ArcFace inference"""
    
    def __init__(self, num_classes, has_arcface=True, config=None):
        super().__init__()
        self.has_arcface = has_arcface
        self.config = config or Config()
        
        # Load backbone
        self.backbone = timm.create_model(
            config.BACKBONE_MODEL, 
            pretrained=False
        )
        
        backbone_dim = self.backbone.num_features
        
        # Attention pooling
        self.pooling = nn.Sequential(
            nn.Linear(backbone_dim, 128),
            nn.Tanh(),
            nn.Linear(128, 1)
        )
        
        # Embedding layer
        self.embedding_fc = nn.Sequential(
            nn.Linear(backbone_dim, 512)
        )
        
        # Classification head
        if has_arcface:
            self.arcface_head = nn.Linear(512, num_classes, bias=False)
            # Initialize ArcFace parameters
            m = self.config.ARCFACE_M
            self.register_buffer('cos_m', torch.tensor(np.cos(m)))
            self.register_buffer('sin_m', torch.tensor(np.sin(m)))
            self.register_buffer('th', torch.tensor(np.cos(np.pi - m)))
            self.register_buffer('mm', torch.tensor(np.sin(np.pi - m) * m))
            self.s = self.config.ARCFACE_S
        else:
            self.head = nn.Linear(512, num_classes)
    
    def forward(self, x, return_features=False):
        """Consistent return type (always tuple)"""
        # Extract features
        features = self.backbone.forward_features(x)
        
        # Handle dict output (newer timm versions)
        if isinstance(features, dict):
            features = features.get("x_norm_patchtokens", features.get("x", None))
            if features is None:
                raise RuntimeError("Could not extract features from BEiT backbone")
        
        # Apply attention pooling
        if features.dim() == 3:
            if features.size(1) > 1:
                features_no_cls = features[:, 1:, :]
            else:
                features_no_cls = features
            
            attention_weights = self.pooling(features_no_cls)
            attention_weights = torch.softmax(attention_weights, dim=1)
            features = torch.sum(features_no_cls * attention_weights, dim=1)
        elif features.dim() == 2:
            pass
        else:
            raise RuntimeError(f"Unexpected feature shape: {features.shape}")
        
        # Get embeddings
        embeddings = self.embedding_fc(features)
        
        if return_features:
            return embeddings, None
        
        # Get logits with proper ArcFace inference
        if self.has_arcface:
            embeddings_norm = F.normalize(embeddings, p=2, dim=1)
            weights_norm = F.normalize(self.arcface_head.weight, p=2, dim=1)
            cosine = F.linear(embeddings_norm, weights_norm)
            logits = cosine * self.s
        else:
            logits = self.head(embeddings)
        
        return embeddings, logits


# ============================================================================
# HYBRID CLASSIFIER
# ============================================================================

class HybridFishClassifier:
    """Enhanced hybrid classifier with robust database loading"""
    
    def __init__(self, config: Config = None):
        if config is None:
            config = Config()
        
        self.config = config
        print("=" * 70)
        print("Initializing Hybrid Fish Classifier (ENHANCED)...")
        print("=" * 70)
        
        # Load labels
        self.labels = self._load_labels(config.LABELS_FILE)
        self.num_classes = len(self.labels)
        
        self.label_to_id = {name: i for i, name in enumerate(self.labels)}
        
        # Load model
        self.device = config.DEVICE
        print(f"Using device: {self.device}")
        self._load_model(config.CKPT_FILE)
        
        # Load retrieval database if enabled
        self.use_retrieval = config.USE_RETRIEVAL
        if self.use_retrieval and os.path.exists(config.DATABASE_FILE):
            try:
                self._load_database(config.DATABASE_FILE)
                self._build_faiss_index()
                print(f"✓ Retrieval system enabled")
            except Exception as e:
                print(f"⚠ Could not load database: {e}")
                print(f"  Falling back to direct classification only")
                self.use_retrieval = False
                import traceback
                traceback.print_exc()
        else:
            self.use_retrieval = False
            if config.USE_RETRIEVAL:
                print(f"⚠ Database file not found. Using direct classification only.")
        
        # Setup transforms
        self.transform = transforms.Compose([
            transforms.Resize((224, 224)),
            transforms.ToTensor(),
            transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
        ])
        
        print(f"✓ Classifier initialized successfully!")
        print(f"  Classes: {self.num_classes}")
        print(f"  Retrieval: {'Enabled' if self.use_retrieval else 'Disabled'}")
        print("=" * 70)
    
    def _load_labels(self, labels_file: str) -> List[str]:
        """Load labels from JSON file"""
        try:
            with open(labels_file, 'r') as f:
                data = json.load(f)
            
            if isinstance(data, dict) and all(isinstance(v, int) for v in data.values()):
                max_id = max(data.values())
                labels = [''] * (max_id + 1)
                for species_name, class_id in data.items():
                    labels[class_id] = species_name
            elif isinstance(data, list):
                labels = data
            elif 'labels' in data:
                labels = data['labels']
            elif 'classes' in data:
                labels = data['classes']
            else:
                num_classes = data.get('num_of_class', 639)
                labels = [f'Class_{i}' for i in range(num_classes)]
            
            print(f"✓ Labels loaded. Total classes: {len(labels)}")
            return labels
        except Exception as e:
            print(f"✗ Error loading labels: {e}")
            raise
    
    def _load_model(self, checkpoint_path: str):
        """Load the model from checkpoint with strict=False handling"""
        print(f"✓ Loading model from: {checkpoint_path}")
        
        checkpoint = torch.load(checkpoint_path, map_location=torch.device('cpu'))
        
        if isinstance(checkpoint, dict) and 'model_state_dict' in checkpoint:
            state_dict = checkpoint['model_state_dict']
        else:
            state_dict = checkpoint
        
        # Detect architecture
        has_arcface = any('arcface_head.' in k for k in state_dict.keys())
        
        # Create model
        self.model = HybridFishModel(self.num_classes, has_arcface, self.config)
        
        # Load weights with detailed reporting
        try:
            missing, unexpected = self.model.load_state_dict(state_dict, strict=False)
            
            if missing or unexpected:
                print(f"  - Weight loading details:")
                if missing:
                    print(f"    - Missing keys: {len(missing)}")
                    if len(missing) <= 5:
                        for key in missing:
                            print(f"      • {key}")
                if unexpected:
                    print(f"    - Unexpected keys: {len(unexpected)}")
                    if len(unexpected) <= 5:
                        for key in unexpected:
                            print(f"      • {key}")
                print(f"  - This is normal for models with optimizer states or extra metadata")
        except Exception as e:
            print(f"  ⚠ Partial weight loading: {str(e)[:150]}")
        
        self.model.to(self.device)
        self.model.eval()
        
        arch_type = "ArcFace Cosine" if has_arcface else "Standard Linear"
        print(f"  ✓ Architecture: BEiT-V2 + {arch_type}")
    
    def _load_database(self, database_path: str):
        """Enhanced database loading with robust label mapping"""
        print(f"✓ Loading retrieval database from: {database_path}")
        
        data = torch.load(database_path)
        self.db_embeddings = data['embeddings'].numpy().astype("float32")
        self.db_labels = data['labels']  # Keep as-is initially
        
        if len(self.db_embeddings) == 0:
            raise RuntimeError("Empty retrieval database")
        
        # Normalize database embeddings
        norms = np.linalg.norm(self.db_embeddings, axis=1, keepdims=True)
        self.db_embeddings = self.db_embeddings / (norms + 1e-8)
        
        # Analyze database structure
        print(f"  - Database structure analysis:")
        print(f"    - Embeddings shape: {self.db_embeddings.shape}")
        print(f"    - Labels type: {type(self.db_labels)}")
        
        # Convert db_labels to numpy array of integers if needed
        if isinstance(self.db_labels, list):
            self.db_labels = np.array(self.db_labels)
        
        # Check if labels are already integers or need conversion
        if self.db_labels.dtype.kind in ['U', 'S', 'O']:  # String types
            print(f"    - Labels are strings, mapping to class IDs...")
            # Map species names to class IDs
            numeric_labels = []
            unmapped_count = 0
            
            for label in self.db_labels:
                if isinstance(label, (np.ndarray, list)):
                    label = label[0] if len(label) > 0 else "Unknown"
                label_str = str(label)
                
                if label_str in self.label_to_id:
                    numeric_labels.append(self.label_to_id[label_str])
                else:
                    # Try to find partial match
                    found = False
                    for known_label, class_id in self.label_to_id.items():
                        if label_str.lower() in known_label.lower() or known_label.lower() in label_str.lower():
                            numeric_labels.append(class_id)
                            found = True
                            break
                    
                    if not found:
                        unmapped_count += 1
                        numeric_labels.append(0)  # Default to first class
            
            self.db_labels = np.array(numeric_labels, dtype=np.int64)
            
            if unmapped_count > 0:
                print(f"    ⚠ {unmapped_count} labels couldn't be mapped (assigned to class 0)")
        else:
            print(f"    - Labels are numeric")
            self.db_labels = self.db_labels.astype(np.int64)
        
        # Build label mapping dictionary
        self.db_id_to_label = {}
        keys_data = data.get('labels_keys', {})
        
        if keys_data:
            print(f"    - Processing {len(keys_data)} label key entries...")
            for key, value in keys_data.items():
                try:
                    # Handle different key-value structures
                    if isinstance(value, dict):
                        if 'label' in value:
                            label_id = int(value['label'])
                            if isinstance(key, str) and key in self.label_to_id:
                                self.db_id_to_label[label_id] = key
                            elif 0 <= label_id < len(self.labels):
                                self.db_id_to_label[label_id] = self.labels[label_id]
                    elif isinstance(value, (int, np.integer)):
                        label_id = int(value)
                        if isinstance(key, str) and key in self.label_to_id:
                            self.db_id_to_label[label_id] = key
                        elif 0 <= label_id < len(self.labels):
                            self.db_id_to_label[label_id] = self.labels[label_id]
                except (ValueError, TypeError, KeyError) as e:
                    continue
        
        # Fill in missing mappings from main labels
        unique_label_ids = np.unique(self.db_labels)
        for label_id in unique_label_ids:
            label_id = int(label_id)
            if label_id not in self.db_id_to_label:
                if 0 <= label_id < len(self.labels):
                    self.db_id_to_label[label_id] = self.labels[label_id]
                else:
                    self.db_id_to_label[label_id] = f"Class_{label_id}"
        
        print(f"  ✓ Database loaded successfully:")
        print(f"    - Total embeddings: {len(self.db_embeddings)}")
        print(f"    - Unique species in DB: {len(unique_label_ids)}")
        print(f"    - Label mappings created: {len(self.db_id_to_label)}")
        print(f"    - Embeddings normalized: Yes")
    
    def _build_faiss_index(self):
        """Build FAISS index with optional GPU acceleration"""
        self.faiss_index = faiss.IndexFlatIP(self.db_embeddings.shape[1])
        
        if self.config.USE_GPU_FAISS and self.config.DEVICE == "cuda":
            try:
                res = faiss.StandardGpuResources()
                self.faiss_index = faiss.index_cpu_to_gpu(res, 0, self.faiss_index)
                print(f"  ✓ FAISS GPU acceleration enabled")
            except Exception as e:
                print(f"  ℹ FAISS using CPU (GPU failed: {e})")
        
        self.faiss_index.add(self.db_embeddings)
        print(f"  ✓ FAISS index built with {len(self.db_embeddings)} vectors")
    
    def _get_retrieval_scores(self, embeddings: torch.Tensor) -> Dict[str, float]:
        """Get retrieval scores with proper normalization"""
        if not self.use_retrieval:
            return {}
        
        embeddings_np = embeddings.cpu().numpy().astype("float32")
        
        # Normalize query embeddings
        norms = np.linalg.norm(embeddings_np, axis=1, keepdims=True)
        embeddings_np = embeddings_np / (norms + 1e-8)
        
        # Search
        distances, indices = self.faiss_index.search(
            embeddings_np, 
            self.config.RETRIEVAL_TOP_K
        )
        
        # Aggregate scores by species
        species_scores = defaultdict(lambda: {'total': 0.0, 'count': 0})
        
        for dist_batch, idx_batch in zip(distances, indices):
            for dist, idx in zip(dist_batch, idx_batch):
                if dist >= self.config.RETRIEVAL_MIN_SIMILARITY:
                    label_id = int(self.db_labels[idx])
                    species_name = self.db_id_to_label.get(label_id, f"Unknown_{label_id}")
                    species_scores[species_name]['total'] += float(dist)
                    species_scores[species_name]['count'] += 1
        
        # Calculate averages and clamp to [0, 1]
        result = {}
        for species, data in species_scores.items():
            avg_score = data['total'] / data['count']
            result[species] = max(0.0, min(1.0, avg_score))
        
        return result
    
    def predict_top_k(self, image_path: str, top_k: int = None) -> List[PredictionResult]:
        """Predict top K species with properly calibrated hybrid scores"""
        if top_k is None:
            top_k = self.config.TOP_K_PREDICTIONS
        
        # Load and preprocess image
        image = Image.open(image_path).convert('RGB')
        image_tensor = self.transform(image).unsqueeze(0).to(self.device)
        
        # Forward pass
        with torch.no_grad():
            embeddings, logits = self.model(image_tensor)
            logits = logits.squeeze(0)
            
            if hasattr(self.model, 's'):
                cosine = logits / self.model.s
            else:
                cosine = logits
            
            direct_scores = (cosine + 1.0) / 2.0
        
        # Get retrieval scores
        retrieval_scores = {}
        if self.use_retrieval:
            retrieval_scores = self._get_retrieval_scores(embeddings)
        
        # Compute hybrid scores for ALL classes
        all_scores = []
        for idx in range(len(direct_scores)):
            species_name = self.labels[idx] if idx < len(self.labels) else f"Class_{idx}"
            direct_score = float(direct_scores[idx])
            retrieval_score = retrieval_scores.get(species_name, 0.0)
            
            if self.use_retrieval and retrieval_score > 0:
                combined_score = (
                    self.config.DIRECT_WEIGHT * direct_score +
                    self.config.RETRIEVAL_WEIGHT * retrieval_score
                ) / (self.config.DIRECT_WEIGHT + self.config.RETRIEVAL_WEIGHT)
            else:
                combined_score = direct_score
            
            all_scores.append((combined_score, idx, direct_score, retrieval_score, species_name))
        
        # Sort by combined score and get top K
        all_scores.sort(key=lambda x: x[0], reverse=True)
        top_predictions = all_scores[:top_k]
        
        # Build prediction results
        predictions = []
        max_combined_score = top_predictions[0][0] if top_predictions else 0.0
        
        for combined_score, idx, direct_score, retrieval_score, species_name in top_predictions:
            predictions.append(PredictionResult(
                species=species_name,
                class_id=idx,
                confidence=combined_score,
                direct_score=direct_score,
                retrieval_score=retrieval_score
            ))
        
        # Add high-scoring retrieval-only candidates
        if self.use_retrieval and max_combined_score > 0:
            existing_species = {p.species for p in predictions}
            retrieval_only_cap = max_combined_score * self.config.RETRIEVAL_ONLY_MAX_RATIO
            
            for species, score in retrieval_scores.items():
                if species not in existing_species and score > 0.5:
                    class_id = self.label_to_id.get(species, -1)
                    
                    combined_score = min(
                        (self.config.RETRIEVAL_WEIGHT * score) / 
                        (self.config.DIRECT_WEIGHT + self.config.RETRIEVAL_WEIGHT),
                        retrieval_only_cap
                    )
                    
                    predictions.append(PredictionResult(
                        species=species,
                        class_id=class_id,
                        confidence=combined_score,
                        direct_score=0.0,
                        retrieval_score=score
                    ))
        
        # Re-sort and return final top K
        predictions.sort(key=lambda x: x.confidence, reverse=True)
        return predictions[:top_k]


# ============================================================================
# PROCESSING PIPELINE
# ============================================================================

def load_existing_results(output_file):
    """Load existing JSON results"""
    if os.path.exists(output_file):
        try:
            with open(output_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠ Could not load existing results: {e}")
            return []
    return []


def image_already_processed(image_path, existing_results):
    """Check using relative path instead of just filename"""
    rel_path = os.path.basename(image_path)
    return any(
        result.get('Image Name') == rel_path or 
        result.get('Image Path') == rel_path 
        for result in existing_results
    )


def move_analyzed_image(image_path, output_dir):
    """Move analyzed image to output directory"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    src = Path(image_path)
    dst = output_dir / src.name

    if dst.exists():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = output_dir / f"{src.stem}_{timestamp}{src.suffix}"

    shutil.move(str(src), str(dst))
    return dst


def main():
    """Main execution function"""
    
    # Configuration
    config = Config()
    
    # Create output directory
    Path(config.OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    
    try:
        # Initialize classifier
        print("\nInitializing Enhanced Hybrid Fish Classifier...\n")
        classifier = HybridFishClassifier(config)
        
        # Load existing results
        existing_results = load_existing_results(config.OUTPUT_FILE)
        print(f"\n✓ Loaded {len(existing_results)} existing result(s)\n")
        
        # Find all images
        print(f"Scanning directory: {config.INPUT_DIR}")
        image_files = []
        for ext in config.IMAGE_EXTENSIONS:
            image_files.extend(glob.glob(os.path.join(config.INPUT_DIR, f"*{ext}")))
        image_files = sorted(image_files)
        
        if not image_files:
            print(f"✗ No image files found in {config.INPUT_DIR}")
            return
        
        print(f"✓ Found {len(image_files)} image(s)\n")
        
        # Process each image
        skipped_count = 0
        processed_count = 0
        start_time = time.time()
        
        for idx, image_path in enumerate(image_files, 1):
            filename = os.path.basename(image_path)
            
            # Check if already processed
            if image_already_processed(image_path, existing_results):
                print(f"[{idx}/{len(image_files)}] Skipping: {filename} (already processed)")
                skipped_count += 1
                continue
            
            print(f"[{idx}/{len(image_files)}] Processing: {filename}")
            
            try:
                # Predict
                predictions = classifier.predict_top_k(
                    image_path, 
                    top_k=config.TOP_K_PREDICTIONS
                )
                
                # Filter by threshold
                filtered_predictions = [
                    p for p in predictions 
                    if p.confidence >= config.CONFIDENCE_THRESHOLD
                ]
                
                # Create result entry
                if len(filtered_predictions) == 1:
                    result_entry = {
                        "Image Name": filename,
                        "Species Predicted": filtered_predictions[0].species,
                        "Confidence Score": filtered_predictions[0].confidence
                    }
                else:
                    result_entry = {
                        "Image Name": filename,
                        "Species Count": len(filtered_predictions),
                        "Species Detected": [p.to_dict() for p in filtered_predictions]
                    }
                
                existing_results.append(result_entry)
                processed_count += 1
                
                # Move image
                moved_path = move_analyzed_image(image_path, config.OUTPUT_DIR_IMAGE)
                print(f"  → Image moved to: {moved_path}")
                
                # Display results
                species_info = "\n".join([
                    f"    • {p.species:<30} "
                    f"| conf={p.confidence:6.2%} "
                    f"| direct={p.direct_score:6.3f} "
                    f"| retrieval={p.retrieval_score:6.3f}"
                    for p in filtered_predictions
                ])

                print("  ✓ Detected:")
                print(species_info)
                print()
                
            except Exception as e:
                print(f"  ✗ Error processing {filename}: {e}\n")
                import traceback
                traceback.print_exc()
        
        # Save results
        with open(config.OUTPUT_FILE, 'w') as f:
            json.dump(existing_results, f, indent=2)
        
        elapsed_time = time.time() - start_time
        
        # Print summary
        print("=" * 70)
        print(f"✓ Analysis complete!")
        print(f"  Total images processed: {processed_count}")
        print(f"  Total images skipped: {skipped_count}")
        print(f"  Total results in file: {len(existing_results)}")
        print(f"  Time elapsed: {elapsed_time:.2f} seconds")
        if processed_count > 0:
            print(f"  Avg time per image: {elapsed_time/processed_count:.2f}s")
        print(f"  Results saved to: {config.OUTPUT_FILE}")
        print("=" * 70)
        
    except FileNotFoundError as e:
        print(f"✗ File not found: {e}")
    except Exception as e:
        print(f"✗ An error occurred: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()