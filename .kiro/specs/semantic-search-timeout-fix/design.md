# Design Document: Semantic Search Timeout Fix

## Overview

This design addresses timeout issues in the semantic search functionality by implementing intelligent batching, graceful degradation, progress tracking, optimization, caching, and configurable limits. The solution maintains backward compatibility while significantly improving performance and reliability for large codebases.

The core strategy is to:
1. Replace fixed file-count batching with size-aware batching
2. Add timeout protection that returns partial results instead of failing
3. Implement chunk-level caching to avoid re-embedding unchanged content
4. Add progress tracking for visibility into long operations
5. Optimize chunking to reduce the number of embeddings needed
6. Make all limits configurable via the config module

## Architecture

The solution modifies the existing CodebaseIndex class in codebase_index.py and adds new configuration options to config.py. The architecture follows these principles:

- **Incremental Processing**: Process files in batches with checkpoints between batches
- **Fail-Safe Design**: Always return partial results rather than failing completely
- **Cache-First**: Check cache before re-embedding to minimize API calls
- **Observable**: Provide progress callbacks for long-running operations
- **Configurable**: All limits and timeouts are configurable with sensible defaults

### Key Components

1. **BatchPlanner**: Determines optimal batches based on file sizes
2. **TimeoutManager**: Tracks elapsed time and enforces timeout limits
3. **ChunkCache**: Manages chunk-level content hashes and embeddings
4. **ProgressTracker**: Reports progress through callbacks
5. **ConfigManager**: Provides configuration values with defaults


## Components and Interfaces

### 1. Configuration Extensions (config.py)

Add new configuration options to AppConfig:

```python
# Semantic search timeout and batching configuration
semantic_search_timeout: int = int(os.getenv("SEMANTIC_SEARCH_TIMEOUT", "60"))
semantic_search_max_batch_bytes: int = int(os.getenv("SEMANTIC_SEARCH_MAX_BATCH_BYTES", "5242880"))  # 5MB
semantic_search_max_chunks_per_file: int = int(os.getenv("SEMANTIC_SEARCH_MAX_CHUNKS_PER_FILE", "100"))
semantic_search_min_chunk_size: int = int(os.getenv("SEMANTIC_SEARCH_MIN_CHUNK_SIZE", "50"))
semantic_search_enable_chunk_cache: bool = os.getenv("SEMANTIC_SEARCH_ENABLE_CHUNK_CACHE", "true").lower() == "true"
```

### 2. BatchPlanner

Responsible for creating size-aware batches of files to process:

```python
class BatchPlanner:
    def __init__(self, max_batch_bytes: int, max_files_per_batch: int = 50):
        self.max_batch_bytes = max_batch_bytes
        self.max_files_per_batch = max_files_per_batch
    
    def plan_batches(self, files_with_sizes: List[Tuple[str, int]]) -> List[List[str]]:
        """
        Create batches of files that respect both size and count limits.
        Returns list of batches, where each batch is a list of file paths.
        """
        pass
```

### 3. TimeoutManager

Tracks elapsed time and determines when to stop processing:

```python
class TimeoutManager:
    def __init__(self, timeout_seconds: float):
        self.timeout_seconds = timeout_seconds
        self.start_time = time.time()
    
    def is_timeout(self) -> bool:
        """Check if timeout has been exceeded."""
        return (time.time() - self.start_time) >= self.timeout_seconds
    
    def remaining_time(self) -> float:
        """Get remaining time in seconds."""
        return max(0, self.timeout_seconds - (time.time() - self.start_time))
```

### 4. ChunkCache

Manages chunk-level content hashes and cached embeddings:

```python
@dataclass
class CachedChunk:
    path: str
    start_line: int
    end_line: int
    content_hash: str
    embedding: List[float]

class ChunkCache:
    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir
        self.cache: Dict[str, CachedChunk] = {}
    
    def get_cached_embedding(self, path: str, start_line: int, end_line: int, 
                            content_hash: str) -> Optional[List[float]]:
        """Get cached embedding if content hash matches."""
        pass
    
    def store_embedding(self, path: str, start_line: int, end_line: int,
                       content_hash: str, embedding: List[float]) -> None:
        """Store embedding with content hash."""
        pass
    
    def load_from_disk(self) -> None:
        """Load cached chunks from disk."""
        pass
    
    def save_to_disk(self) -> None:
        """Save cached chunks to disk."""
        pass
```

### 5. ProgressTracker

Reports progress through callbacks:

```python
@dataclass
class ProgressInfo:
    current_batch: int
    total_batches: int
    current_file: str
    files_processed: int
    total_files: int
    estimated_time_remaining: float

class ProgressTracker:
    def __init__(self, total_files: int, callback: Optional[Callable[[ProgressInfo], None]] = None):
        self.total_files = total_files
        self.callback = callback
        self.files_processed = 0
        self.start_time = time.time()
    
    def report_progress(self, current_batch: int, total_batches: int, current_file: str) -> None:
        """Report progress through callback if provided."""
        pass
```

### 6. Modified CodebaseIndex Methods

#### retrieve_with_refresh (Enhanced)

```python
def retrieve_with_refresh(
    self, 
    query: str, 
    top_k: int = 10, 
    backend: Optional[Any] = None,
    timeout: Optional[float] = None,
    on_progress: Optional[Callable[[ProgressInfo], None]] = None
) -> Tuple[List[CodeChunk], Dict[str, Any]]:
    """
    Semantic search with staleness check and timeout protection.
    
    Returns:
        Tuple of (chunks, metadata) where metadata contains:
        - 'completed': bool indicating if all files were processed
        - 'files_processed': int count of files successfully processed
        - 'files_skipped': int count of files skipped due to timeout
        - 'timeout_occurred': bool indicating if timeout was hit
    """
    pass
```

#### _refresh_files_with_timeout (New)

```python
def _refresh_files_with_timeout(
    self,
    files_to_refresh: List[str],
    backend: Optional[Any],
    timeout_manager: TimeoutManager,
    progress_tracker: ProgressTracker
) -> Tuple[List[CodeChunk], int, int]:
    """
    Refresh files with timeout protection and progress tracking.
    
    Returns:
        Tuple of (new_chunks, files_processed, files_skipped)
    """
    pass
```

#### _optimize_chunks (New)

```python
def _optimize_chunks(self, chunks: List[CodeChunk], max_chunks: int) -> List[CodeChunk]:
    """
    Optimize chunks by merging small adjacent chunks and limiting total count.
    """
    pass
```

#### _get_file_sizes (New)

```python
def _get_file_sizes(self, files: List[str], backend: Optional[Any]) -> List[Tuple[str, int]]:
    """
    Get file sizes for batch planning.
    Returns list of (file_path, size_in_bytes) tuples.
    """
    pass
```


## Data Models

### Enhanced CodeChunk

The existing CodeChunk dataclass is extended with a content_hash field:

```python
@dataclass
class CodeChunk:
    path: str
    start_line: int
    end_line: int
    kind: str
    name: str
    text: str
    embedding: Optional[List[float]] = None
    content_hash: Optional[str] = None  # NEW: for chunk-level caching
```

### Metadata Storage Format

The meta.json file is extended to include chunk cache information:

```json
{
  "file_hashes": {"path/to/file.py": "abc123..."},
  "file_mtimes": {"path/to/file.py": 1234567890.0},
  "file_imports": {"path/to/file.py": ["module1", "module2"]},
  "chunk_cache": {
    "path/to/file.py:10:50": {
      "content_hash": "def456...",
      "embedding": [0.1, 0.2, ...]
    }
  }
}
```

### Batch Structure

Batches are represented as lists of file paths with associated metadata:

```python
@dataclass
class Batch:
    files: List[str]
    total_bytes: int
    batch_number: int
```

### Progress Information

```python
@dataclass
class ProgressInfo:
    current_batch: int
    total_batches: int
    current_file: str
    files_processed: int
    total_files: int
    estimated_time_remaining: float
    percent_complete: float
```

### Retrieval Metadata

```python
@dataclass
class RetrievalMetadata:
    completed: bool
    files_processed: int
    files_skipped: int
    timeout_occurred: bool
    processing_time: float
    batches_processed: int
    total_batches: int
```


## Correctness Properties

A property is a characteristic or behavior that should hold true across all valid executions of a system—essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.

### Property 1: Batch Planning Respects Size and Count Constraints

*For any* set of files with their sizes, when planning batches, all resulting batches should respect both the maximum byte size limit and the maximum file count limit, and the total set of files across all batches should equal the input set.

**Validates: Requirements 1.1, 1.2, 1.3**

### Property 2: Sequential Batch Processing

*For any* sequence of batches, batch N+1 should not begin processing until batch N has completed processing.

**Validates: Requirements 1.4**

### Property 3: Timeout Stops Processing and Returns Partial Results

*For any* timeout value and set of files to process, when the timeout is exceeded, processing should stop and return all successfully processed chunks along with metadata indicating which files were processed and which were skipped.

**Validates: Requirements 2.1, 2.2, 2.3**

### Property 4: Timeout Preserves Chunks Without Embeddings

*For any* timeout scenario, all chunks that were created before the timeout should be preserved in the index, even if their embeddings were not computed.

**Validates: Requirements 2.5**

### Property 5: Progress Tracking Accuracy

*For any* processing operation with a progress callback, the progress information should accurately reflect the current state: percentage should equal (files_processed / total_files) * 100, current_file should be the file being processed, and the callback should be invoked after each batch completes.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

### Property 6: Time Estimation Reasonableness

*For any* progress update, the estimated time remaining should be non-negative and should decrease as processing progresses (assuming consistent processing speed).

**Validates: Requirements 3.5**

### Property 7: Chunk Count Limit Enforcement

*For any* file, the number of chunks produced after optimization should not exceed the configured maximum chunks per file.

**Validates: Requirements 4.1, 4.2**

### Property 8: Minimum Chunk Size Filtering

*For any* set of chunks that have embeddings, all chunks should meet the minimum size requirement and should not be composed entirely of whitespace.

**Validates: Requirements 4.3**

### Property 9: Batch Embedding Efficiency

*For any* set of chunks to embed, the embedding function should be called with multiple chunks at once rather than individual chunks (when multiple chunks are available).

**Validates: Requirements 4.4**

### Property 10: Cache Hit for Unchanged Chunks

*For any* chunk that is re-indexed with the same content, the cached embedding should be reused rather than calling the embedding service again.

**Validates: Requirements 5.1, 5.2**

### Property 11: Chunk Cache Round-Trip Consistency

*For any* set of chunks with embeddings and content hashes, saving to disk then loading from disk should preserve all chunk embeddings and content hashes.

**Validates: Requirements 5.3, 5.4**

### Property 12: Cache Invalidation on Content Change

*For any* chunk whose content changes between indexing operations, the cache should not return the old embedding for the new content.

**Validates: Requirements 5.5**

### Property 13: Configuration Validation

*For any* invalid configuration value (negative timeout, zero or negative batch size, etc.), the system should reject the configuration and raise an appropriate error.

**Validates: Requirements 6.5**


## Error Handling

### Timeout Errors

- **Graceful Degradation**: When timeout occurs, return partial results with metadata indicating completion status
- **Logging**: Log timeout events with details about files processed and skipped
- **No Exceptions**: Timeouts should not raise exceptions; they should be handled internally

### Configuration Errors

- **Validation**: Validate all configuration values at initialization
- **Clear Messages**: Provide clear error messages for invalid configurations
- **Fail Fast**: Reject invalid configurations before processing begins

### File Access Errors

- **Skip and Continue**: If a file cannot be read, log the error and continue with remaining files
- **Track Failures**: Include failed files in metadata returned to caller
- **No Silent Failures**: Always log file access errors

### Embedding Service Errors

- **Retry Logic**: Implement exponential backoff for transient embedding failures
- **Partial Success**: If some chunks embed successfully and others fail, keep the successful ones
- **Error Propagation**: If embedding fails completely, preserve chunks without embeddings for later retry

### Cache Errors

- **Degradation**: If cache cannot be loaded, proceed without cache (re-embed everything)
- **Save Failures**: If cache cannot be saved, log error but don't fail the operation
- **Corruption Handling**: If cache is corrupted, rebuild from scratch

### Backward Compatibility Errors

- **Format Migration**: Automatically migrate old format data to new format
- **Missing Fields**: Treat missing optional fields (like content_hash) as None
- **Version Detection**: Detect old format by absence of chunk_cache key in meta.json


## Testing Strategy

### Dual Testing Approach

This feature requires both unit tests and property-based tests to ensure comprehensive coverage:

- **Unit tests**: Verify specific examples, edge cases, and error conditions
- **Property tests**: Verify universal properties across all inputs

Both testing approaches are complementary and necessary. Unit tests catch concrete bugs in specific scenarios, while property tests verify general correctness across a wide range of inputs.

### Property-Based Testing

We will use **Hypothesis** (Python's property-based testing library) to implement the correctness properties defined above. Each property test will:

- Run a minimum of 100 iterations to ensure comprehensive input coverage
- Generate random but realistic test data (file lists, sizes, timeouts, etc.)
- Reference the design document property it validates using a comment tag

**Tag Format**: `# Feature: semantic-search-timeout-fix, Property N: [property description]`

**Property Test Configuration**:
```python
from hypothesis import given, settings
import hypothesis.strategies as st

@settings(max_examples=100)
@given(...)
def test_property_N(...):
    # Feature: semantic-search-timeout-fix, Property N: [description]
    ...
```

### Unit Testing Focus

Unit tests should focus on:

1. **Specific Examples**: Test concrete scenarios like "3 files totaling 10MB split into 2 batches"
2. **Edge Cases**: 
   - Single file larger than batch limit
   - Empty file list
   - All files already cached
   - Timeout on first batch
   - Zero timeout
3. **Integration Points**:
   - Interaction with embedding service
   - File backend operations
   - Configuration loading
4. **Error Conditions**:
   - Invalid configuration values
   - File read failures
   - Embedding service failures
   - Cache corruption

### Test Coverage Requirements

- All public methods in modified classes must have unit tests
- All 13 correctness properties must have property-based tests
- Edge cases identified in requirements must have dedicated unit tests
- Backward compatibility scenarios must have integration tests

### Mock Strategy

- **Embedding Service**: Mock to avoid API calls and control timing
- **File Backend**: Mock to control file sizes and simulate failures
- **Time**: Mock time.time() to test timeout behavior deterministically
- **Progress Callbacks**: Use mock callbacks to verify invocation

### Performance Testing

While not part of automated tests, manual performance testing should verify:

- Large codebases (1000+ files) complete within reasonable time
- Memory usage remains bounded during processing
- Cache significantly reduces re-indexing time
- Progress updates don't significantly slow processing

