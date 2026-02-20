# Requirements Document

## Introduction

The semantic search functionality in codebase_index.py experiences timeout issues when processing large files. The retrieve_with_refresh method can process up to 50 files at once, and each file is chunked and embedded. For large files, this processing can exceed acceptable time limits, causing the operation to fail completely rather than returning partial results. This feature addresses these timeout issues through intelligent batching, graceful degradation, progress tracking, optimization, caching, and configurable limits.

## Glossary

- **Semantic_Search**: The codebase indexing and retrieval system that chunks code files and embeds them for similarity search
- **Chunking**: The process of splitting a file into semantic units (functions, classes, blocks) for embedding
- **Embedding**: Converting text chunks into vector representations using the embedding model
- **Batch**: A group of files or chunks processed together in a single operation
- **Graceful_Degradation**: Returning partial results when full processing cannot complete within time limits
- **Cache**: Stored embeddings for unchanged chunks to avoid re-processing
- **Content_Hash**: A hash of file content used to detect changes and determine if re-embedding is needed

## Requirements

### Requirement 1: Intelligent Size-Based Batching

**User Story:** As a developer, I want the semantic search to batch files based on their sizes, so that large files don't cause the entire operation to timeout.

#### Acceptance Criteria

1. WHEN processing multiple files, THE Semantic_Search SHALL calculate the total size of files in each batch before processing
2. WHEN a batch would exceed a size threshold, THE Semantic_Search SHALL split it into smaller batches
3. WHEN determining batch size, THE Semantic_Search SHALL consider both file count and total byte size
4. THE Semantic_Search SHALL process batches sequentially to avoid overwhelming the embedding service
5. WHEN a single file exceeds the maximum batch size, THE Semantic_Search SHALL process it in its own batch

### Requirement 2: Timeout Protection with Graceful Degradation

**User Story:** As a developer, I want the semantic search to return partial results when processing takes too long, so that I get some results instead of a complete failure.

#### Acceptance Criteria

1. WHEN processing time exceeds a configured timeout, THE Semantic_Search SHALL stop processing remaining batches
2. WHEN a timeout occurs, THE Semantic_Search SHALL return all successfully processed chunks
3. WHEN returning partial results, THE Semantic_Search SHALL indicate which files were processed and which were skipped
4. THE Semantic_Search SHALL log timeout events with details about what was processed
5. WHEN a timeout occurs during embedding, THE Semantic_Search SHALL preserve chunks without embeddings for later processing

### Requirement 3: Progress Tracking for Long Operations

**User Story:** As a developer, I want to see progress updates during long-running semantic search operations, so that I know the system is working and not frozen.

#### Acceptance Criteria

1. WHEN processing multiple batches, THE Semantic_Search SHALL report progress after each batch completes
2. WHEN processing files, THE Semantic_Search SHALL report the current file being processed
3. THE Semantic_Search SHALL report the percentage of files processed
4. WHEN a progress callback is provided, THE Semantic_Search SHALL invoke it with current progress information
5. THE Semantic_Search SHALL include estimated time remaining in progress updates

### Requirement 4: Optimized Chunking and Embedding

**User Story:** As a developer, I want the chunking and embedding process to be optimized for large files, so that processing completes faster.

#### Acceptance Criteria

1. WHEN chunking large files, THE Semantic_Search SHALL limit the number of chunks per file
2. WHEN a file produces too many chunks, THE Semantic_Search SHALL merge smaller chunks together
3. THE Semantic_Search SHALL skip embedding for chunks that are too small or contain only whitespace
4. WHEN embedding multiple chunks, THE Semantic_Search SHALL batch embed requests to the embedding service
5. THE Semantic_Search SHALL reuse chunk boundaries from previous processing when file content is similar

### Requirement 5: Chunk-Level Caching

**User Story:** As a developer, I want unchanged chunks to be cached, so that re-indexing is faster and doesn't re-embed content that hasn't changed.

#### Acceptance Criteria

1. WHEN a file is re-indexed, THE Semantic_Search SHALL identify which chunks have changed based on content
2. WHEN a chunk's content matches a previously embedded chunk, THE Semantic_Search SHALL reuse the existing embedding
3. THE Semantic_Search SHALL store chunk content hashes alongside embeddings
4. WHEN loading from disk, THE Semantic_Search SHALL load cached chunk embeddings
5. THE Semantic_Search SHALL invalidate cached chunks when their content changes

### Requirement 6: Configurable Processing Limits

**User Story:** As a developer, I want to configure timeout and size limits for semantic search, so that I can tune performance for my specific codebase.

#### Acceptance Criteria

1. THE Semantic_Search SHALL read timeout configuration from the config module
2. THE Semantic_Search SHALL read maximum batch size configuration from the config module
3. THE Semantic_Search SHALL read maximum chunks per file configuration from the config module
4. WHEN configuration values are not provided, THE Semantic_Search SHALL use sensible defaults
5. THE Semantic_Search SHALL validate configuration values and reject invalid settings

### Requirement 7: Backward Compatibility

**User Story:** As a developer, I want the semantic search improvements to work with existing indexes, so that I don't need to rebuild my entire codebase index.

#### Acceptance Criteria

1. WHEN loading an existing index, THE Semantic_Search SHALL read chunks without chunk-level hashes
2. WHEN processing files with old-format cached data, THE Semantic_Search SHALL migrate to the new format
3. THE Semantic_Search SHALL maintain compatibility with the existing retrieve and retrieve_with_refresh API
4. WHEN new configuration options are not set, THE Semantic_Search SHALL behave like the current implementation
5. THE Semantic_Search SHALL preserve existing file hash and mtime tracking
