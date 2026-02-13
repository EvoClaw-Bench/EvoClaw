# Single Milestone Implementation

You are an expert Software Engineer implementing a single development task.

## Environment

- **Working Directory**: `/testbed` (the repository root)

## Task

Implement the code changes specified in the following SRS (Software Requirements Specification).

### SRS Content

{srs_content}

## Workflow

1. **Analyze**: Read the SRS carefully and explore the codebase to understand existing patterns
2. **Plan**: Design your implementation approach
3. **Implement**: Make the required code changes
4. **Verify**: Ensure your changes are complete and follow existing patterns
5. **Commit & Tag**:
   ```bash
   git add .
   git commit -m "Implement {milestone_id}"
   git tag agent-impl-{milestone_id}
   ```

**IMPORTANT**: The `git tag agent-impl-{milestone_id}` command signals task completion.
